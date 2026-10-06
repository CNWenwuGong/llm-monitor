"""测试引擎：单条对话测试、基准套件、并发负载压测。

统一在这里处理
--------------
* 指标打点：TTFT / E2E / TPS / Token 数 / 分位数 / 峰值资源
* 结果落库：test_runs + request_details
* 实时进度：通过 WebSocket 广播 progress / run_done
* 安全保护：错误率与显存双阈值熔断，避免压测把机器打挂
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

import httpx

from . import chat, collector, config, db
from .probe import BaseAdapter, estimate_tokens, messages_to_text
from .probe import _prefill_ms_from_meta as prefill_ms_from_meta
from .probe import is_truncated, normalize_finish_reason
from .ws import broadcaster

log = logging.getLogger("llm-monitor.tester")

PROMPTS_PATH: Path = config.BACKEND_DIR / "prompts" / "benchmarks.json"

# --------------------------------------------------------------------------
# Prompt 集加载
# --------------------------------------------------------------------------
_prompt_cache: Optional[Dict[str, Any]] = None

_FILLER = (
    "在本地部署大语言模型时，推理性能受到显存带宽、KV Cache 容量与批处理策略的共同约束。"
    "服务在接收到请求后需要先完成 Tokenization，再把输入序列喂入模型的前向计算图中。"
    "Prefill 阶段负责并行处理整个 Prompt，Decode 阶段则以自回归方式逐个生成 Token。"
    "当上下文长度增长时，KV Cache 的显存占用近似线性上升，这也是长上下文场景的主要瓶颈来源。"
    "工程师通常会用连续批处理（continuous batching）把多个请求拼成一批，以提升 GPU 利用率。"
    "在排队时间上，一旦并发数超过服务的最佳批大小，请求的尾延迟会迅速恶化。"
    "因此容量规划的核心，是找到吞吐与尾延迟之间的平衡点，并用压测数据加以验证。"
)


def load_prompts(refresh: bool = False) -> Dict[str, Any]:
    global _prompt_cache
    if _prompt_cache is None or refresh:
        with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
            _prompt_cache = json.load(f)
    return _prompt_cache


def list_suites() -> List[Dict[str, Any]]:
    data = load_prompts()
    cases = {c["id"]: c for c in data["cases"]}
    out = []
    for s in data["suites"]:
        out.append({
            "id": s["id"], "name": s["name"], "desc": s.get("desc", ""),
            "case_count": len(s["cases"]),
            "cases": [{"id": cid, "label": cases.get(cid, {}).get("label", cid),
                       "category": cases.get(cid, {}).get("category", "")}
                      for cid in s["cases"]],
        })
    return out


def list_cases() -> List[Dict[str, Any]]:
    return load_prompts()["cases"]


def make_context(target_tokens: int) -> str:
    """按目标 Token 数生成填充文本（用于长上下文测试）。"""
    parts: List[str] = []
    total = 0
    i = 0
    while total < target_tokens:
        s = _FILLER[i % len(_FILLER)]
        parts.append(s)
        total += estimate_tokens(s)
        i += 1
    return "".join(parts)


def build_messages(case: Dict[str, Any], extra_system: str = "") -> List[Dict[str, str]]:
    prompt = case.get("prompt", "")
    if case.get("context_tokens"):
        prompt = prompt.replace("{context}", make_context(int(case["context_tokens"])))
    msgs: List[Dict[str, str]] = []
    system = case.get("system") or extra_system
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


# --------------------------------------------------------------------------
# 统计工具
# --------------------------------------------------------------------------
def percentile(values: Sequence[float], p: float) -> Optional[float]:
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    if len(vs) == 1:
        return round(vs[0], 2)
    k = (len(vs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return round(vs[int(k)], 2)
    return round(vs[lo] + (vs[hi] - vs[lo]) * (k - lo), 2)


def box_stats(values: Sequence[float]) -> Dict[str, Any]:
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return {"min": None, "q1": None, "median": None, "q3": None, "max": None, "mean": None}
    return {
        "min": round(vs[0], 2),
        "q1": percentile(vs, 0.25),
        "median": percentile(vs, 0.50),
        "q3": percentile(vs, 0.75),
        "max": round(vs[-1], 2),
        "mean": round(sum(vs) / len(vs), 2),
    }


def summarize(details: Sequence[Dict[str, Any]], duration_ms: float = 0.0,
              concurrency: int = 1) -> Dict[str, Any]:
    oks = [d for d in details if d.get("status") == "ok"]
    errs = [d for d in details if d.get("status") != "ok"]
    e2e = [d.get("e2e_ms") for d in oks if d.get("e2e_ms")]
    ttft = [d.get("ttft_ms") for d in oks if d.get("ttft_ms")]
    tps = [d.get("tps") for d in oks if d.get("tps")]
    pt = [d.get("prompt_tokens") or 0 for d in oks]
    ct = [d.get("completion_tokens") or 0 for d in oks]
    total = len(details)
    dur_s = duration_ms / 1000 if duration_ms else 0.0

    # ---- Prefill / Decode 拆分（效率指标） ----
    # 优先使用后端自报的 prefill 耗时（Ollama 的 prompt_eval_duration 更准），
    # 缺失时回退到 TTFT 近似。两者口径不同，故在返回里标注来源。
    prefill_num = 0
    prefill_den = 0.0
    decode_num = 0
    decode_den = 0.0
    prefill_from_native = False
    for d in oks:
        pn = d.get("prompt_tokens")
        cn = d.get("completion_tokens")
        pm = d.get("prefill_ms")
        if pm and pn:
            prefill_from_native = True
        elif pm is None:
            # 没有后端自报值时用 TTFT 近似 prefill，但仅限流式请求
            # （非流式 TTFT≡E2E，会把 decode 时间算进 prefill，必须弃用）
            ttft = d.get("ttft_ms")
            e2e = d.get("e2e_ms")
            if ttft and (e2e is None or e2e - ttft >= 5.0):
                pm = ttft
            else:
                pm = None
        if pn and pm:
            prefill_num += pn
            prefill_den += pm
        # Decode 段 = E2E - TTFT。非流式请求的 TTFT 恒等于 E2E，此时 decode 窗口
        # 不可测（会被 0.001s 的钳制放大成虚高吞吐），必须跳过而不是编一个数。
        if cn and d.get("e2e_ms") and d.get("ttft_ms") is not None:
            gen_ms = d["e2e_ms"] - d["ttft_ms"]
            if gen_ms >= 5.0:
                decode_num += cn
                decode_den += gen_ms
    prefill_tps = round(prefill_num / (prefill_den / 1000), 2) if prefill_den else None
    decode_tps = round(decode_num / (decode_den / 1000), 2) if decode_den else None

    # 整体吞吐：所有成功请求产出的 token 总量 / 墙钟时长（含排队与并发）
    total_tokens = sum(ct)
    throughput = round(total_tokens / dur_s, 2) if dur_s > 0 and total_tokens else None

    stats = {
        "total": total,
        "ok": len(oks),
        "errors": len(errs),
        "error_rate": round(len(errs) / total, 4) if total else 0.0,
        "avg_tps": round(sum(tps) / len(tps), 2) if tps else None,
        "max_tps": round(max(tps), 2) if tps else None,
        "min_tps": round(min(tps), 2) if tps else None,
        "avg_ttft_ms": round(sum(ttft) / len(ttft), 2) if ttft else None,
        "p50_ttft_ms": percentile(ttft, 0.50),
        "p90_ttft_ms": percentile(ttft, 0.90),
        "p95_ttft_ms": percentile(ttft, 0.95),
        "p99_ttft_ms": percentile(ttft, 0.99),
        "avg_e2e_ms": round(sum(e2e) / len(e2e), 2) if e2e else None,
        "p50_e2e_ms": percentile(e2e, 0.50),
        "p90_e2e_ms": percentile(e2e, 0.90),
        "p95_e2e_ms": percentile(e2e, 0.95),
        "p99_e2e_ms": percentile(e2e, 0.99),
        "prefill_tps": prefill_tps,
        "decode_tps": decode_tps,
        "prefill_source": "native" if prefill_from_native else (
            "ttft_proxy" if prefill_den else None),
        "throughput_tps": throughput,
        "avg_prompt_tokens": round(sum(pt) / len(pt), 1) if pt else None,
        "avg_completion_tokens": round(sum(ct) / len(ct), 1) if ct else None,
        "total_tokens": total_tokens,
        "total_prompt_tokens": sum(pt),
        "qps": round(len(oks) / dur_s, 3) if dur_s > 0 else None,
        "concurrency": concurrency,
        "duration_ms": round(duration_ms, 1),
    }
    return {
        "stats": stats,
        "boxplot": {"ttft": box_stats(ttft), "e2e": box_stats(e2e), "tps": box_stats(tps)},
    }


def recommend_concurrency(stages: Sequence[Dict[str, Any]],
                          latency_ratio: float = 1.5,
                          err_limit: float = 0.02,
                          gain_ratio: float = 0.6) -> Dict[str, Any]:
    """根据阶梯压测结果给出建议并发上限（拐点前的一档）。

    三条判据，任意一条命中即认为该档位已越过拐点：

    1. **尾延迟膨胀**：该档 p95 超过低负载基线（各档 p95 的最小值）的
       ``latency_ratio`` 倍。默认 1.5 倍——尾延迟相对空载膨胀超过 50% 就该止步。
    2. **错误率升高**：超过 ``err_limit``（默认 2%）。
    3. **吞吐收益递减**：并发翻 k 倍而 QPS 增长不足 k × ``gain_ratio``（默认 0.6）。

    建议值取拐点前一档；若全程未命中，则返回最高测试档位，并明确提示
    「仍近似线性，可继续加压」，不谎称已找到拐点。
    """
    rows = [s for s in stages if (s.get("qps") or 0) > 0]
    if not rows:
        return {"ok": False, "reason": "缺少有效的阶梯压测数据"}
    rows = sorted(rows, key=lambda s: s.get("concurrency") or 0)

    p95s = [s.get("p95_e2e_ms") or s.get("p99_e2e_ms") for s in rows]
    p95s = [p for p in p95s if p]
    baseline = min(p95s) if p95s else None

    def brief(s: Dict[str, Any]) -> Dict[str, Any]:
        return {"concurrency": s.get("concurrency"), "qps": s.get("qps"),
                "p95_e2e_ms": s.get("p95_e2e_ms") or s.get("p99_e2e_ms"),
                "error_rate": s.get("error_rate")}

    for i, s in enumerate(rows):
        p95 = s.get("p95_e2e_ms") or s.get("p99_e2e_ms")
        er = s.get("error_rate") or 0.0
        qps = s.get("qps") or 0.0
        hit: Optional[str] = None
        if baseline and p95 and p95 > baseline * latency_ratio:
            hit = (f"尾延迟膨胀到基线的 {p95 / baseline:.2f} 倍"
                   f"（{p95:.0f}ms vs {baseline:.0f}ms），超过 "
                   f"{latency_ratio:.1f} 倍阈值")
        elif er > err_limit:
            hit = f"错误率 {er * 100:.1f}% 超过 {err_limit * 100:.0f}%"
        elif i > 0:
            prev = rows[i - 1]
            prev_qps = prev.get("qps") or 0.0
            prev_conc = prev.get("concurrency") or 1
            conc = s.get("concurrency") or 1
            if prev_qps > 0 and conc > prev_conc:
                factor = conc / prev_conc
                gain = qps / prev_qps
                if gain < factor * gain_ratio:
                    hit = (f"并发翻 {factor:.0f} 倍而 QPS 只增 {gain:.2f} 倍，"
                           f"吞吐收益明显递减")
        if hit:
            safe = rows[i - 1] if i > 0 else rows[0]
            return {
                "ok": True, "recommended": safe.get("concurrency"),
                "knee_concurrency": s.get("concurrency"), "reason": hit,
                "baseline_p95_ms": round(baseline, 1) if baseline else None,
                "latency_ratio_limit": latency_ratio,
                "safe_stage": brief(safe), "knee_stage": brief(s),
                "stages": len(rows),
            }

    last = rows[-1]
    return {
        "ok": True, "recommended": last.get("concurrency"),
        "knee_concurrency": None,
        "reason": "全程尾延迟与错误率都在阈值内、吞吐随并发近似线性增长，"
                  "未找到拐点；建议值取最高测试档位，可继续加压验证",
        "baseline_p95_ms": round(baseline, 1) if baseline else None,
        "latency_ratio_limit": latency_ratio,
        "safe_stage": brief(last), "knee_stage": None, "stages": len(rows),
    }


# --------------------------------------------------------------------------
# 运行上下文与任务注册表
# --------------------------------------------------------------------------
class RunContext:
    def __init__(self, run_id: int, run_type: str) -> None:
        self.run_id = run_id
        self.run_type = run_type
        self.stop_flag = False
        self.stop_reason: Optional[str] = None
        self.started = time.time()
        self.started_perf = time.perf_counter()
        self.results: List[Dict[str, Any]] = []
        self.qps_deque: deque = deque()
        self.stage_index = 0
        self.stage_plan: List[Tuple[int, int]] = []
        self.concurrency = 1
        self.peak_vram = 0.0
        self.peak_util = 0.0
        self._util_sum = 0.0
        self._util_n = 0
        self.task: Optional[asyncio.Task] = None
        self.expected_total = 0

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_perf) * 1000

    def note_resource(self, sample: Dict[str, Any]) -> None:
        v = sample.get("vram_used_mb")
        u = sample.get("gpu_util")
        if v:
            self.peak_vram = max(self.peak_vram, float(v))
        if u is not None:
            self.peak_util = max(self.peak_util, float(u))
            self._util_sum += float(u)
            self._util_n += 1

    @property
    def avg_util(self) -> float:
        return round(self._util_sum / self._util_n, 1) if self._util_n else 0.0

    def qps(self, window: float = 5.0) -> float:
        cutoff = time.time() - window
        while self.qps_deque and self.qps_deque[0][0] < cutoff:
            self.qps_deque.popleft()
        return round(len(self.qps_deque) / window, 2)

    def error_rate(self) -> float:
        if not self.results:
            return 0.0
        bad = sum(1 for r in self.results if r.get("status") != "ok")
        return bad / len(self.results)

    def progress_payload(self) -> Dict[str, Any]:
        oks = [r for r in self.results if r.get("status") == "ok"]
        e2e = [r["e2e_ms"] for r in oks if r.get("e2e_ms")]
        ttft = [r["ttft_ms"] for r in oks if r.get("ttft_ms")]
        return {
            "type": "progress",
            "run_id": self.run_id,
            "run_type": self.run_type,
            "elapsed_ms": round(self.elapsed_ms(), 1),
            "done": len(self.results),
            "total": self.expected_total,
            "stage_index": self.stage_index,
            "stage_count": len(self.stage_plan),
            "concurrency": self.concurrency,
            "current_qps": self.qps(),
            "avg_latency_ms": round(sum(e2e) / len(e2e), 1) if e2e else 0,
            "p99_latency_ms": percentile(e2e, 0.99) or 0,
            "avg_ttft_ms": round(sum(ttft) / len(ttft), 1) if ttft else 0,
            "error_rate": round(self.error_rate(), 4),
            "ok": len(oks),
            "errors": len(self.results) - len(oks),
            "vram_used_mb": collector.last_sample.get("vram_used_mb"),
            "gpu_util": collector.last_sample.get("gpu_util"),
            "stop_reason": self.stop_reason,
        }


_active_runs: Dict[int, RunContext] = {}


def get_active(run_id: int) -> Optional[RunContext]:
    return _active_runs.get(run_id)


def list_active() -> List[Dict[str, Any]]:
    """当前正在运行的所有任务进度（供前端刷新后恢复状态）。"""
    out: List[Dict[str, Any]] = []
    for ctx in list(_active_runs.values()):
        if ctx.task is not None and ctx.task.done():
            continue
        out.append(ctx.progress_payload())
    return out


def request_stop(run_id: int) -> bool:
    ctx = _active_runs.get(run_id)
    if not ctx:
        return False
    ctx.stop_flag = True
    ctx.stop_reason = ctx.stop_reason or "manual"
    return True


def stop_all() -> int:
    n = 0
    for ctx in list(_active_runs.values()):
        ctx.stop_flag = True
        ctx.stop_reason = ctx.stop_reason or "manual"
        n += 1
    return n


# --------------------------------------------------------------------------
# 单条对话测试（SSE 流式）
# --------------------------------------------------------------------------
async def stream_single(conn: Dict[str, Any], adapter: BaseAdapter, model: str,
                        messages: List[Dict[str, str]], params: Dict[str, Any],
                        save: bool = True,
                        chat_ctx: Optional[Dict[str, Any]] = None) -> AsyncIterator[Dict[str, Any]]:
    """流式执行一次对话，逐事件产出给前端（SSE）。

    事件序列: start → delta* → metrics → done

    ``chat_ctx`` 非空时表示这是「对话测试」的一轮：本轮结束后会把提问与回答
    写进会话（``chat_messages``），从而让下一轮能带上历史 —— 也就是「记忆」。
    start 事件里同时回传上下文占用明细，让前端能显示载入了多少轮、丢了多少轮。

    注意：客户端中途断开会触发 ``GeneratorExit``。此时生成器不能再 ``yield``，
    但仍需同步落库收尾，把该次运行标记为 ``interrupted``，
    否则记录会永久停留在 ``running`` 状态。
    """
    prompt_text = messages_to_text(messages)
    label = (messages[-1].get("content", "") if messages else "")[:80]
    run_id = None
    if save:
        run_id = db.create_run(
            conn.get("id"), conn.get("name", ""), model, "single", label,
            1, collector.env_snapshot(),
            {"params": params, "messages": messages, "stream": True},
        )
    ctx = chat_ctx or {}
    yield {"event": "start", "run_id": run_id, "model": model,
           "context": ctx.get("context"), "session_id": ctx.get("session_id"),
           "mode": ctx.get("mode", "chat")}

    collector.stats.request_started()
    t0 = time.perf_counter()
    first_sample = dict(collector.last_sample)
    state: Dict[str, Any] = {
        "ttft_ms": 0.0, "chunks": [], "prompt_tokens": None,
        "completion_tokens": None, "meta": {}, "status": "ok", "error": None,
    }

    def finalize(aborted: bool) -> Dict[str, Any]:
        """计算本次请求指标并落库（同步，可在 GeneratorExit 路径调用）。"""
        e2e_ms = (time.perf_counter() - t0) * 1000
        text = "".join(state["chunks"])
        ttft_ms = state["ttft_ms"] or e2e_ms
        completion_tokens = state["completion_tokens"]
        prompt_tokens = state["prompt_tokens"]
        if completion_tokens is None:
            completion_tokens = estimate_tokens(text)
        if prompt_tokens is None:
            prompt_tokens = estimate_tokens(prompt_text)
        gen_ms = max(1.0, e2e_ms - ttft_ms)
        tps = round(completion_tokens / (gen_ms / 1000), 2) if completion_tokens else 0.0
        status = "interrupted" if aborted else state["status"]
        max_tokens_cfg = int(params.get("max_tokens") or 0)
        finish_reason = normalize_finish_reason(state["meta"])
        truncated = is_truncated(state["meta"], completion_tokens, max_tokens_cfg)
        collector.stats.request_finished(
            ttft_ms, tps, completion_tokens,
            ok=(status == "ok"), e2e_ms=e2e_ms)
        detail = {
            "worker_id": 0, "case_label": label, "ttft_ms": round(ttft_ms, 2),
            "e2e_ms": round(e2e_ms, 2), "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "tps": tps,
            "prefill_ms": prefill_ms_from_meta(state["meta"]),
            "status": status, "error": state["error"],
        }
        if run_id is not None:
            db.insert_details(run_id, [detail])
            db.finish_run(run_id, {
                "duration_ms": round(e2e_ms, 1),
                "concurrency": 1,
                "gpu_peak_vram": first_sample.get("vram_used_mb"),
                "gpu_peak_util": first_sample.get("gpu_util"),
                "gpu_avg_util": first_sample.get("gpu_util"),
                **summarize([detail], e2e_ms),
            }, status=status)
        out = {
            **detail,
            "text": text,
            "gen_ms": round(gen_ms, 2),
            "token_utilization": (
                round(completion_tokens / max_tokens_cfg, 4)
                if max_tokens_cfg > 0 else None),
            "max_tokens_cfg": max_tokens_cfg,
            "finish_reason": finish_reason or ("length" if truncated else "stop"),
            "truncated": bool(truncated),
            "meta": state["meta"],
            "gpu": {
                "gpu_util": first_sample.get("gpu_util"),
                "vram_used_mb": first_sample.get("vram_used_mb"),
                "vram_total_mb": first_sample.get("vram_total_mb"),
            },
        }
        # 对话测试：把这一轮写进会话，供下一轮作为历史（记忆）带上
        if ctx.get("session_id"):
            try:
                ids = chat.record_turn(
                    int(ctx["session_id"]), ctx.get("user_text") or "",
                    text if status == "ok" else (text or ""),
                    run_id=run_id,
                    echo_user=bool(ctx.get("echo_user", True)),
                    continued=bool(ctx.get("continued")),
                    truncated=bool(truncated), finish_reason=out["finish_reason"],
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    metrics={"context": ctx.get("context"), "ttft_ms": detail["ttft_ms"],
                             "e2e_ms": detail["e2e_ms"], "tps": tps,
                             "status": status, "truncated": bool(truncated),
                             # 记下本次下发的上限（0 = 不限制）。落库后前端才能区分
                             # 「被我们设的 max_tokens 截断」还是「服务端自己限制了长度」。
                             "max_tokens": max_tokens_cfg,
                             "completion_tokens": completion_tokens,
                             "finish_reason": out["finish_reason"]},
                )
                out["session_message_ids"] = ids
                sess_row = db.get_chat_session(int(ctx["session_id"]))
                if sess_row:
                    chat.ensure_title(sess_row, ctx.get("user_text") or "")
                out["session"] = chat.session_public(
                    db.get_chat_session(int(ctx["session_id"])) or {})
            except Exception as exc:  # noqa: BLE001
                log.warning("对话落库失败: %s", exc)
        return out

    try:
        async for chunk in adapter.chat_stream(
            model, messages,
            temperature=float(params.get("temperature", 0.7)),
            top_p=float(params.get("top_p", 0.9)),
            max_tokens=int(params.get("max_tokens", 512)),
            num_ctx=params.get("num_ctx"),
            stream=bool(params.get("stream", True)),
        ):
            if chunk.get("delta"):
                if not state["ttft_ms"]:
                    state["ttft_ms"] = (time.perf_counter() - t0) * 1000
                state["chunks"].append(chunk["delta"])
                yield {"event": "delta", "text": chunk["delta"]}
            if chunk.get("prompt_tokens") is not None:
                state["prompt_tokens"] = chunk["prompt_tokens"]
            if chunk.get("completion_tokens") is not None:
                state["completion_tokens"] = chunk["completion_tokens"]
            if chunk.get("meta"):
                state["meta"].update(chunk["meta"])
    except (GeneratorExit, asyncio.CancelledError):
        # 客户端断开 / 任务取消：生成器已不可再 yield，只做同步收尾
        finalize(aborted=True)
        raise
    except Exception as exc:  # noqa: BLE001
        state["status"] = "error"
        state["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("单条测试失败: %s", state["error"])

    metrics = finalize(aborted=False)
    yield {"event": "metrics", "data": metrics}
    yield {"event": "done", "run_id": run_id,
           "status": metrics["status"], "error": metrics["error"]}


# --------------------------------------------------------------------------
# 基准测试套件
# --------------------------------------------------------------------------
async def _progress_ticker(ctx: RunContext, stop_event: asyncio.Event) -> None:
    """测试过程中每秒广播一次进度 + 熔断检查。"""
    while not stop_event.is_set() and not ctx.stop_flag:
        sample = collector.last_sample
        if sample:
            ctx.note_resource(sample)
        await broadcaster.broadcast(ctx.progress_payload())
        # 熔断保护：错误率 / 显存
        if ctx.run_type == "loadtest" and len(ctx.results) >= 8:
            if ctx.error_rate() > config.BREAKER_ERROR_RATE:
                ctx.stop_reason = f"错误率 {ctx.error_rate() * 100:.1f}% 超过阈值 " \
                                  f"{config.BREAKER_ERROR_RATE * 100:.0f}%"
                ctx.stop_flag = True
            vram = sample.get("vram_used_mb")
            total = sample.get("vram_total_mb")
            if vram and total and (vram / total * 100) > config.BREAKER_VRAM_PCT:
                ctx.stop_reason = f"显存占用 {vram / total * 100:.1f}% 超过阈值 " \
                                  f"{config.BREAKER_VRAM_PCT:.0f}%"
                ctx.stop_flag = True
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def run_benchmark(run_id: int, conn: Dict[str, Any], adapter: BaseAdapter,
                        model: str, suite_id: str, overrides: Dict[str, Any],
                        case_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """串行跑完一个基准套件，落库并广播结果。"""
    data = load_prompts()
    cases = {c["id"]: c for c in data["cases"]}
    if case_ids:
        selected = [cases[cid] for cid in case_ids if cid in cases]
    else:
        suite = next((s for s in data["suites"] if s["id"] == suite_id), None)
        if not suite:
            raise ValueError(f"套件 {suite_id} 不存在")
        selected = [cases[cid] for cid in suite["cases"] if cid in cases]
    if not selected:
        raise ValueError("套件内没有可用用例")

    ctx = RunContext(run_id, "benchmark")
    ctx.expected_total = len(selected)
    ctx.stage_plan = [(1, 0)]
    ctx.task = asyncio.current_task()
    _active_runs[run_id] = ctx
    stop_event = asyncio.Event()
    ticker = asyncio.create_task(_progress_ticker(ctx, stop_event))
    case_results: List[Dict[str, Any]] = []
    all_details: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=config.DEFAULT_REQUEST_TIMEOUT) as client:
        for idx, case in enumerate(selected):
            if ctx.stop_flag:
                break
            ctx.stage_index = idx
            ctx.concurrency = 1
            params = {**case.get("params", {}), **overrides}
            messages = build_messages(case, overrides.get("system", ""))
            try:
                r = await adapter.chat_collect(model, messages, client=client,
                                               temperature=float(params.get("temperature", 0.7)),
                                               top_p=float(params.get("top_p", 0.9)),
                                               max_tokens=int(params.get("max_tokens", 512)),
                                               num_ctx=params.get("num_ctx"),
                                               stream=True)
                detail = {
                    "worker_id": 0, "case_label": case["label"],
                    "ttft_ms": r["ttft_ms"], "e2e_ms": r["e2e_ms"],
                    "prompt_tokens": r["prompt_tokens"],
                    "completion_tokens": r["completion_tokens"], "tps": r["tps"],
                    "prefill_ms": r.get("prefill_ms"),
                    "status": "ok", "error": None,
                }
                case_results.append({
                    "case_id": case["id"], "label": case["label"],
                    "category": case["category"], "context_tokens": case.get("context_tokens"),
                    **{k: detail[k] for k in ("ttft_ms", "e2e_ms", "prompt_tokens",
                                              "completion_tokens", "tps")},
                    "status": "ok", "preview": r["text"][:200],
                })
            except Exception as exc:  # noqa: BLE001
                detail = {
                    "worker_id": 0, "case_label": case["label"], "ttft_ms": None,
                    "e2e_ms": None, "prompt_tokens": None, "completion_tokens": None,
                    "tps": None, "status": "error",
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
                case_results.append({
                    "case_id": case["id"], "label": case["label"],
                    "category": case["category"], "status": "error",
                    "error": detail["error"],
                })
            all_details.append(detail)
            ctx.results.append(detail)
            ctx.qps_deque.append((time.time(), detail["status"] == "ok"))
            db.insert_details(run_id, [detail])
            await broadcaster.broadcast(ctx.progress_payload())

    stop_event.set()
    ticker.cancel()
    ctx.note_resource(collector.last_sample)
    duration_ms = ctx.elapsed_ms()
    summary = summarize(all_details, duration_ms, 1)
    payload = {
        "duration_ms": round(duration_ms, 1),
        "concurrency": 1,
        "max_qps": None,
        "gpu_peak_vram": round(ctx.peak_vram, 1) or None,
        "gpu_peak_util": round(ctx.peak_util, 1) or None,
        "gpu_avg_util": ctx.avg_util or None,
        "cases": case_results,
        "suite_id": suite_id,
        **summary,
    }
    status = "stopped" if ctx.stop_flag else "done"
    db.finish_run(run_id, payload, status=status)
    _active_runs.pop(run_id, None)
    await broadcaster.broadcast({
        "type": "run_done", "run_id": run_id, "run_type": "benchmark",
        "status": status, "summary": summary["stats"], "cases": case_results,
    })
    return payload


# --------------------------------------------------------------------------
# 并发负载压测
# --------------------------------------------------------------------------
def build_stage_plan(cfg: Dict[str, Any]) -> List[Tuple[int, int]]:
    """生成 (并发数, 持续秒数) 阶梯计划。"""
    mode = cfg.get("mode", "fixed")
    duration = int(cfg.get("duration", 60))
    if mode == "stage":
        start = max(1, int(cfg.get("start_concurrency", 1)))
        max_c = max(start, int(cfg.get("max_concurrency", 16)))
        stage_secs = max(5, int(cfg.get("stage_seconds", 30)))
        plan: List[Tuple[int, int]] = []
        c = start
        while c <= max_c:
            plan.append((c, stage_secs))
            c *= 2
        limit = int(cfg.get("duration", 0))
        if limit > 0:  # 若指定总时长，则截断阶梯
            acc, trimmed = 0, []
            for conc, secs in plan:
                if acc >= limit:
                    break
                take = min(secs, limit - acc)
                trimmed.append((conc, take))
                acc += take
            plan = trimmed or [(start, limit)]
        return plan
    return [(max(1, int(cfg.get("concurrency", 8))), duration)]


async def run_loadtest(run_id: int, conn: Dict[str, Any], adapter: BaseAdapter,
                       model: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """并发压测主流程：阶梯加压 → 逐级统计 → 熔断保护 → 汇总报告。"""
    plan = build_stage_plan(cfg)
    prompts: List[str] = list(cfg.get("prompts") or [])
    if not prompts:
        prompts = ["请用三句话说明什么是本地大模型的推理性能瓶颈。"]
    max_tokens = int(cfg.get("max_tokens", 256))
    temperature = float(cfg.get("temperature", 0.7))

    ctx = RunContext(run_id, "loadtest")
    ctx.stage_plan = plan
    ctx.expected_total = 0
    ctx.task = asyncio.current_task()
    _active_runs[run_id] = ctx
    stop_event = asyncio.Event()
    ticker = asyncio.create_task(_progress_ticker(ctx, stop_event))
    stage_summaries: List[Dict[str, Any]] = []

    limits = httpx.Limits(max_connections=max(64, max(c for c, _ in plan) * 2),
                          max_keepalive_connections=max(32, max(c for c, _ in plan)))
    async with httpx.AsyncClient(timeout=config.DEFAULT_REQUEST_TIMEOUT, limits=limits) as client:
        for index, (conc, secs) in enumerate(plan):
            if ctx.stop_flag:
                break
            ctx.stage_index = index
            ctx.concurrency = conc
            stage_start_count = len(ctx.results)
            deadline = time.perf_counter() + secs
            sem = asyncio.Semaphore(conc)

            async def worker(wid: int) -> None:
                while not ctx.stop_flag and time.perf_counter() < deadline:
                    async with sem:
                        if ctx.stop_flag or time.perf_counter() >= deadline:
                            return
                        prompt = random.choice(prompts)
                        messages = [{"role": "user", "content": prompt}]
                        collector.stats.request_started()
                        t0 = time.perf_counter()
                        try:
                            r = await adapter.chat_collect(
                                model, messages, client=client,
                                temperature=temperature, max_tokens=max_tokens, stream=True)
                            rec = {
                                "worker_id": wid, "case_label": f"c{conc}",
                                "ttft_ms": r["ttft_ms"], "e2e_ms": r["e2e_ms"],
                                "prompt_tokens": r["prompt_tokens"],
                                "completion_tokens": r["completion_tokens"],
                                "tps": r["tps"], "prefill_ms": r.get("prefill_ms"),
                                "status": "ok", "error": None,
                            }
                            collector.stats.request_finished(
                                r["ttft_ms"], r["tps"], r["completion_tokens"],
                                ok=True, e2e_ms=r["e2e_ms"])
                        except Exception as exc:  # noqa: BLE001
                            e2e = (time.perf_counter() - t0) * 1000
                            rec = {
                                "worker_id": wid, "case_label": f"c{conc}",
                                "ttft_ms": None, "e2e_ms": round(e2e, 2),
                                "prompt_tokens": None, "completion_tokens": None,
                                "tps": None, "status": "error",
                                "error": f"{type(exc).__name__}: {exc}"[:300],
                            }
                            collector.stats.request_finished(ok=False, e2e_ms=e2e)
                        ctx.results.append(rec)
                        ctx.qps_deque.append((time.time(), rec["status"] == "ok"))

            workers = [asyncio.create_task(worker(i)) for i in range(conc)]
            done, pending = await asyncio.wait(workers, timeout=max(120, secs + 60))
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            stage_details = ctx.results[stage_start_count:]
            db.insert_details(run_id, stage_details)  # 逐级落库，避免中途异常丢数据
            st = summarize(stage_details, secs * 1000, conc)
            stage_summaries.append({
                "concurrency": conc, "planned_seconds": secs,
                "total": st["stats"]["total"], "ok": st["stats"]["ok"],
                "errors": st["stats"]["errors"],
                "qps": st["stats"]["qps"],
                "avg_tps": st["stats"]["avg_tps"],
                "avg_ttft_ms": st["stats"]["avg_ttft_ms"],
                "p95_e2e_ms": st["stats"]["p95_e2e_ms"],
                "p99_e2e_ms": st["stats"]["p99_e2e_ms"],
                "avg_e2e_ms": st["stats"]["avg_e2e_ms"],
                "prefill_tps": st["stats"]["prefill_tps"],
                "decode_tps": st["stats"]["decode_tps"],
                "throughput_tps": st["stats"]["throughput_tps"],
                "error_rate": st["stats"]["error_rate"],
            })
            await broadcaster.broadcast({
                "type": "stage_done", "run_id": run_id,
                "stage_index": index, "stage": stage_summaries[-1],
            })

    stop_event.set()
    ticker.cancel()
    ctx.note_resource(collector.last_sample)
    duration_ms = ctx.elapsed_ms()
    all_details = ctx.results
    summary = summarize(all_details, duration_ms, ctx.concurrency or 1)

    best = max((s for s in stage_summaries if s.get("qps")), key=lambda s: s["qps"], default=None)
    max_qps = best["qps"] if best else None
    status = "stopped" if ctx.stop_flag else "done"
    payload = {
        "duration_ms": round(duration_ms, 1),
        "concurrency": ctx.concurrency,
        "max_qps": max_qps,
        "best_stage": best,
        "stages": stage_summaries,
        "stage_plan": plan,
        "stop_reason": ctx.stop_reason,
        "gpu_peak_vram": round(ctx.peak_vram, 1) or None,
        "gpu_peak_util": round(ctx.peak_util, 1) or None,
        "gpu_avg_util": ctx.avg_util or None,
        "breaker": {
            "error_rate_limit": config.BREAKER_ERROR_RATE,
            "vram_limit_pct": config.BREAKER_VRAM_PCT,
        },
        **summary,
        "recommend": recommend_concurrency(stage_summaries),
    }
    db.finish_run(run_id, payload, status=status)
    _active_runs.pop(run_id, None)
    await broadcaster.broadcast({
        "type": "run_done", "run_id": run_id, "run_type": "loadtest",
        "status": status, "summary": summary["stats"],
        "stages": stage_summaries, "max_qps": max_qps,
        "stop_reason": ctx.stop_reason,
    })
    return payload
