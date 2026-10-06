"""推理后端适配层（probe）。

统一接口
--------
所有 Adapter 实现同一个归一化流式接口::

    async def chat_stream(model, messages, **params) -> AsyncIterator[chunk]

其中 ``messages`` 为 ``[{"role": "user"|"system"|"assistant", "content": str}]``，
产出的 ``chunk`` 统一为::

    {"delta": str, "done": bool,
     "prompt_tokens": int|None, "completion_tokens": int|None, "meta": {...}}

上层测试引擎（tester.py）只依赖该接口，不感知后端差异。

已实现后端
----------
* ``ollama``        —— POST /api/chat，原生返回 prompt_eval_count / eval_count
* ``vllm``          —— OpenAI 兼容 /v1/chat/completions + /metrics Prometheus
* ``openai_compat`` —— 通用 OpenAI 兼容端点
* ``llama_cpp``     —— llama.cpp server /completion（含 timings）
* ``demo``          —— 内置模拟后端，无需任何真实推理服务即可完整体验平台
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

import httpx

from . import config

log = logging.getLogger("llm-monitor.probe")


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
_CJK = re.compile(r"[\u4e00-\u9fff]")


def estimate_tokens(text: str) -> int:
    """粗略 Token 估算：中日韩字符按 1 token，其余按 ~4 字符/token。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    others = len(text) - cjk
    return int(cjk + others / 4 + 0.5)


def messages_to_text(messages: Sequence[Dict[str, str]]) -> str:
    parts: List[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"System: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
        else:
            parts.append(f"User: {content}")
    return "\n".join(parts) + "\nAssistant:"


def _sse_payload(line: str) -> Optional[str]:
    if not line:
        return None
    line = line.strip()
    if line.startswith("data:"):
        return line[5:].strip()
    return None


def _prefill_ms_from_meta(meta: Optional[Dict[str, Any]]) -> Optional[float]:
    """从后端自报的 meta 里取出 prefill（Prompt 处理）耗时，单位毫秒。

    * Ollama  ：``prompt_eval_duration`` (ns)
    * llama.cpp：``timings.prompt_ms`` (ms)
    取不到时返回 None，调用方回退到 TTFT 作为代理值。
    """
    if not meta:
        return None
    ns = meta.get("prompt_eval_duration_ns") or meta.get("prompt_eval_duration")
    if ns:
        try:
            return round(float(ns) / 1e6, 2)
        except (TypeError, ValueError):
            pass
    timings = meta.get("timings") or {}
    pm = timings.get("prompt_ms")
    if pm:
        try:
            return round(float(pm), 2)
        except (TypeError, ValueError):
            pass
    return None


# --------------------------------------------------------------------------
# 终止原因归一
# --------------------------------------------------------------------------# 各后端对「为什么停止生成」的叫法不同：
#   Ollama      -> done_reason : stop / length / load
#   OpenAI 兼容 -> finish_reason: stop / length / content_filter（vLLM 同）
#   llama.cpp   -> stop_type   : eos / limit / word
# 统一映射为 stop（自然结束） / length（撞到 max_tokens） / 其它原样保留。
_FINISH_MAP = {
    "stop": "stop", "eos": "stop", "end_turn": "stop", "stop_sequence": "stop",
    "length": "length", "max_tokens": "length", "max_token": "length", "limit": "length",
    "content_filter": "content_filter",
    "word": "stop_word",
}


def normalize_finish_reason(meta: Optional[Dict[str, Any]]) -> str:
    """把各后端的终止原因字段归一成 stop / length / ...（取不到返回空串）。"""
    if not meta:
        return ""
    raw = (meta.get("finish_reason") or meta.get("done_reason")
           or meta.get("stop_type") or "")
    raw = str(raw).strip().lower()
    return _FINISH_MAP.get(raw, raw)


def is_truncated(meta: Optional[Dict[str, Any]],
                 completion_tokens: Optional[int] = None,
                 max_tokens: int = 0) -> bool:
    """判断本次回答是否**被 max_tokens 截断**。

    判据优先级：后端明确自报 ``length`` > 后端自报 ``truncated`` >
    「生成了 max_tokens 个 token」的数量推断（部分后端不给终止原因）。
    """
    if normalize_finish_reason(meta) == "length":
        return True
    if meta and meta.get("truncated"):
        return True
    if max_tokens and max_tokens > 0 and completion_tokens:
        return int(completion_tokens) >= int(max_tokens)
    return False


# 「继续生成」时下发给模型的指令。放在 probe 里是因为：
# 适配层要知道「这一轮是续写」才能正确作答（演示后端据此走续写分支），
# 装配层（chat.py）只是引用这个常量，不重复定义。
CONTINUE_MARK = "请从上一段输出的中断处"
CONTINUE_INSTRUCTION = (
    f"{CONTINUE_MARK}**继续往下写**：衔接上文语义，不要重复已经写过的内容，"
    "也不要重新开头或复述问题。"
)



# --------------------------------------------------------------------------
# 基类
# --------------------------------------------------------------------------
class BaseAdapter:
    backend = "base"
    label = "Base"
    # 该适配器是否需要评测框架把标准答案一并传入（只在内置 demo 上为 True）。
    # 真实后端的 chat_stream 会把未知关键字收进 **params 且不下发到 HTTP，
    # 因此置 False 时标准答案根本不会被传进来，杜绝"泄题"风险。
    hints_gold: bool = False
    supports_ppl: bool = False

    def __init__(self, base_url: str, api_key: str = "", default_model: str = "") -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.default_model = default_model or ""

    # ---- 请求头 ----
    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _client(self, timeout: float = config.DEFAULT_REQUEST_TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, headers=self._headers())

    # ---- 探测 ----
    async def list_models(self) -> List[str]:
        return []

    async def version(self) -> str:
        return ""

    async def probe(self) -> Dict[str, Any]:
        """连通性探测：返回 ok / version / models / latency_ms。"""
        t0 = time.perf_counter()
        try:
            version = await self.version()
            models = await self.list_models()
            return {
                "ok": True,
                "version": version,
                "models": models,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": None,
            }
        except httpx.TimeoutException as exc:
            return {"ok": False, "version": "", "models": [],
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "error": f"连接超时: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "version": "", "models": [],
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "error": f"{type(exc).__name__}: {exc}"}

    async def native_metrics(self) -> Dict[str, Any]:
        return {}

    # ---- 推理 ----
    async def chat_stream(self, model: str, messages: Sequence[Dict[str, str]],
                          client: Optional[httpx.AsyncClient] = None,
                          **params: Any) -> AsyncIterator[Dict[str, Any]]:
        raise NotImplementedError
        yield {}  # pragma: no cover

    # ---- 能力声明 ----
    # supports_ppl / hints_gold 统一在类头部声明，见上方定义。

    # ---- 困惑度 PPL ----
    async def perplexity(self, text: str, mode: str = "teacher_forcing",
                         max_tokens: int = 16, top_k: int = 20,
                         client: Optional[httpx.AsyncClient] = None,
                         **params: Any) -> Dict[str, Any]:
        """计算困惑度。默认实现表示「该后端不支持取对数概率」。

        PPL 需要模型对每个 token 输出对数概率，只有部分后端暴露该能力；
        不支持时返回 ``ok=False`` 并给出原因，前端会明确标注「不可用」，
        而不是拿一个假数字糊弄。
        """
        return {"ok": False, "reason": f"{self.label} 未实现对数概率输出，无法计算 PPL"}

    async def chat_collect(self, model: str, messages: Sequence[Dict[str, str]],
                           client: Optional[httpx.AsyncClient] = None,
                           **params: Any) -> Dict[str, Any]:
        """跑完一次完整推理，返回耗时与 Token 统计（供压测/基准复用）。"""
        t0 = time.perf_counter()
        ttft_ms = 0.0
        text_parts: List[str] = []
        prompt_tokens = completion_tokens = None
        meta: Dict[str, Any] = {}
        async for chunk in self.chat_stream(model, messages, client=client, **params):
            if chunk.get("delta"):
                if not ttft_ms:
                    ttft_ms = (time.perf_counter() - t0) * 1000
                text_parts.append(chunk["delta"])
            if chunk.get("prompt_tokens") is not None:
                prompt_tokens = chunk["prompt_tokens"]
            if chunk.get("completion_tokens") is not None:
                completion_tokens = chunk["completion_tokens"]
            if chunk.get("meta"):
                meta.update(chunk["meta"])
        e2e_ms = (time.perf_counter() - t0) * 1000
        text = "".join(text_parts)
        if not ttft_ms:
            ttft_ms = e2e_ms
        if completion_tokens is None:
            completion_tokens = estimate_tokens(text)
        if prompt_tokens is None:
            prompt_tokens = estimate_tokens(messages_to_text(messages))
        gen_ms = max(1.0, e2e_ms - ttft_ms)
        return {
            "text": text,
            "ttft_ms": round(ttft_ms, 2),
            "e2e_ms": round(e2e_ms, 2),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "tps": round(completion_tokens / (gen_ms / 1000), 2) if completion_tokens else 0.0,
            "meta": meta,
            "prefill_ms": _prefill_ms_from_meta(meta),
        }


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------
class OllamaAdapter(BaseAdapter):
    backend = "ollama"
    label = "Ollama"

    async def version(self) -> str:
        async with self._client(10) as c:
            r = await c.get(f"{self.base_url}/api/version")
            r.raise_for_status()
            return str(r.json().get("version", ""))

    async def list_models(self) -> List[str]:
        async with self._client(15) as c:
            r = await c.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]

    async def native_metrics(self) -> Dict[str, Any]:
        try:
            async with self._client(10) as c:
                r = await c.get(f"{self.base_url}/api/ps")
                r.raise_for_status()
                models = r.json().get("models", [])
                vram = sum(m.get("size_vram", 0) for m in models) / 1048576
                return {
                    "loaded_models": [
                        {
                            "name": m.get("name"),
                            "size_mb": round(m.get("size", 0) / 1048576, 1),
                            "vram_mb": round(m.get("size_vram", 0) / 1048576, 1),
                            "expires_at": m.get("expires_at"),
                        }
                        for m in models
                    ],
                    "loaded_vram_mb": round(vram, 1),
                }
        except Exception:  # noqa: BLE001
            return {}

    async def chat_stream(self, model, messages, client=None, temperature: float = 0.7,
                          top_p: float = 0.9, max_tokens: int = 512,
                          num_ctx: Optional[int] = None, stream: bool = True,
                          **params: Any) -> AsyncIterator[Dict[str, Any]]:
        options: Dict[str, Any] = {"temperature": temperature, "top_p": top_p,
                                  # num_predict = -1 表示不限制输出长度（Ollama 约定）
                                  "num_predict": max_tokens if max_tokens and max_tokens > 0 else -1}
        if num_ctx:
            options["num_ctx"] = num_ctx
        payload = {"model": model or self.default_model, "messages": list(messages),
                   "stream": stream, "options": options}
        own = client is None
        c = client or self._client()
        try:
            if not stream:
                r = await c.post(f"{self.base_url}/api/chat", json=payload)
                r.raise_for_status()
                obj = r.json()
                text = (obj.get("message") or {}).get("content", "")
                yield {"delta": text, "done": True,
                       "prompt_tokens": obj.get("prompt_eval_count"),
                       "completion_tokens": obj.get("eval_count"),
                       "meta": {"eval_duration_ns": obj.get("eval_duration"),
                                "prompt_eval_duration_ns": obj.get("prompt_eval_duration"),
                                "total_duration_ns": obj.get("total_duration"),
                                "done_reason": obj.get("done_reason")}}
                return
            async with c.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    delta = (obj.get("message") or {}).get("content", "")
                    if obj.get("done"):
                        yield {"delta": delta, "done": True,
                               "prompt_tokens": obj.get("prompt_eval_count"),
                               "completion_tokens": obj.get("eval_count"),
                               "meta": {"eval_duration_ns": obj.get("eval_duration"),
                                        "prompt_eval_duration_ns": obj.get("prompt_eval_duration"),
                                        "total_duration_ns": obj.get("total_duration"),
                                        "done_reason": obj.get("done_reason")}}
                    elif delta:
                        yield {"delta": delta, "done": False}
        finally:
            if own:
                await c.aclose()


    supports_ppl = True

    async def perplexity(self, text: str, mode: str = "autoregressive",
                         max_tokens: int = 24, top_k: int = 20,
                         client: Optional[httpx.AsyncClient] = None,
                         **params: Any) -> Dict[str, Any]:
        """Ollama 的 PPL：``/api/generate`` 支持 ``logprobs`` 时取其对数概率。"""
        own = client is None
        c = client or self._client(120)
        try:
            r = await c.post(f"{self.base_url}/api/generate", json={
                "model": self.default_model, "prompt": text, "stream": False,
                "logprobs": True, "options": {"num_predict": max(4, max_tokens),
                                              "temperature": 0},
            })
            r.raise_for_status()
            obj = r.json()
            lps = [float(x["logprob"]) for x in (obj.get("logprobs") or [])
                   if x.get("logprob") is not None]
            if not lps:
                return {"ok": False,
                        "reason": "当前 Ollama 未返回 logprobs（需 ≥0.6 版本并开启 logprobs）"}
            return _ppl_payload(lps, "autoregressive",
                                note=f"对生成的 {len(lps)} 个 token 计算")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"PPL 请求失败: {type(exc).__name__}: {exc}"}
        finally:
            if own:
                await c.aclose()


# --------------------------------------------------------------------------
# OpenAI 兼容（vLLM / 通用）
# --------------------------------------------------------------------------
class OpenAICompatAdapter(BaseAdapter):
    backend = "openai_compat"
    label = "OpenAI 兼容"

    @property
    def root(self) -> str:
        return self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"

    async def version(self) -> str:
        async with self._client(10) as c:
            r = await c.get(f"{self.root}/models")
            r.raise_for_status()
            data = r.json().get("data")
            return f"openai-compat ({len(data)} models)" if isinstance(data, list) else "openai-compat"

    async def list_models(self) -> List[str]:
        async with self._client(15) as c:
            r = await c.get(f"{self.root}/models")
            r.raise_for_status()
            return [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]

    async def native_metrics(self) -> Dict[str, Any]:
        """尽力解析 Prometheus /metrics（vLLM 关键指标）。"""
        try:
            async with self._client(10) as c:
                r = await c.get(f"{self.base_url}/metrics")
                if r.status_code != 200:
                    return {}
                out: Dict[str, Any] = {}
                want = {
                    "vllm:num_requests_running": "num_requests_running",
                    "vllm:num_requests_waiting": "num_requests_waiting",
                    "vllm:gpu_cache_usage_perc": "gpu_cache_usage_perc",
                    "vllm:cpu_cache_usage_perc": "cpu_cache_usage_perc",
                    "vllm:avg_prompt_throughput_toks_per_s": "avg_prompt_throughput",
                    "vllm:avg_generation_throughput_toks_per_s": "avg_generation_throughput",
                }
                for line in r.text.splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    body = line.split("{")[0] if "{" in line else line.split(" ")[0]
                    name = line.split(" ")[0].split("{")[0]
                    for key, short in want.items():
                        if line.startswith(key):
                            try:
                                out[short] = float(line.rsplit(" ", 1)[-1])
                            except ValueError:
                                pass
                    _ = body
                # 累计计数类指标取一次快照
                for metric, short in (("vllm:prompt_tokens_total", "prompt_tokens_total"),
                                      ("vllm:generation_tokens_total", "generation_tokens_total"),
                                      ("vllm:request_success_total", "request_success_total")):
                    for line in r.text.splitlines():
                        if line.startswith(metric):
                            try:
                                out[short] = float(line.rsplit(" ", 1)[-1])
                            except ValueError:
                                pass
                return out
        except Exception:  # noqa: BLE001
            return {}

    async def chat_stream(self, model, messages, client=None, temperature: float = 0.7,
                          top_p: float = 0.9, max_tokens: int = 512,
                          num_ctx: Optional[int] = None, stream: bool = True,
                          **params: Any) -> AsyncIterator[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "messages": list(messages),
            "temperature": temperature,
            "top_p": top_p,
            "stream": stream,
        }
        # max_tokens<=0 表示不限制：直接不带该字段（OpenAI 兼容端点的默认行为）
        if max_tokens and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}
        own = client is None
        c = client or self._client()
        try:
            if not stream:
                r = await c.post(f"{self.root}/chat/completions", json=payload)
                r.raise_for_status()
                obj = r.json()
                choice = (obj.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content", "") or ""
                usage = obj.get("usage") or {}
                yield {"delta": text, "done": True,
                       "prompt_tokens": usage.get("prompt_tokens"),
                       "completion_tokens": usage.get("completion_tokens"),
                       "meta": {"finish_reason": choice.get("finish_reason")}}
                return
            async with c.stream("POST", f"{self.root}/chat/completions", json=payload) as r:
                r.raise_for_status()
                # finish_reason 与 usage 未必落在同一帧：llama.cpp / vLLM 会先发
                # finish_reason，再补一帧仅带 usage 的收尾帧。所以要**记住**最后一次
                # 非空的 finish_reason，否则收尾事件会把它丢成 null。
                last_finish: Optional[str] = None
                async for line in r.aiter_lines():
                    data = _sse_payload(line)
                    if not data or data == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    choices = obj.get("choices") or []
                    delta = ""
                    finish = None
                    if choices:
                        ch = choices[0]
                        delta = (ch.get("delta") or {}).get("content") or ""
                        finish = ch.get("finish_reason")
                    if finish:
                        last_finish = finish
                    usage = obj.get("usage")
                    if usage:
                        yield {"delta": delta or "", "done": True,
                               "prompt_tokens": usage.get("prompt_tokens"),
                               "completion_tokens": usage.get("completion_tokens"),
                               "meta": {"finish_reason": last_finish}}
                        continue
                    if delta:
                        yield {"delta": delta, "done": False}
                    if finish and not usage:
                        yield {"delta": "", "done": True, "meta": {"finish_reason": finish}}
        finally:
            if own:
                await c.aclose()


    supports_ppl = True

    async def perplexity(self, text: str, mode: str = "auto", max_tokens: int = 24,
                         top_k: int = 20, client: Optional[httpx.AsyncClient] = None,
                         **params: Any) -> Dict[str, Any]:
        own = client is None
        c = client or self._client()
        try:
            return await ppl_openai_compat(c, self.root, self.default_model,
                                           text, mode, max_tokens, top_k)
        finally:
            if own:
                await c.aclose()


class VLLMAdapter(OpenAICompatAdapter):
    backend = "vllm"
    label = "vLLM"


# --------------------------------------------------------------------------
# llama.cpp server
# --------------------------------------------------------------------------
# base_url -> 该 llama.cpp 服务是否带对话模板（能否走 /v1/chat/completions）。
# 进程级缓存：适配器是按请求新建的，不缓存就会每次都去探一次 /props。
_LLAMA_CHAT_TEMPLATE: Dict[str, bool] = {}


class LlamaCppAdapter(BaseAdapter):
    backend = "llama_cpp"
    label = "llama.cpp server"

    async def _chat_template_available(self) -> bool:
        """服务端是否带对话模板 —— 决定能不能走 /v1/chat/completions。

        为什么要看这个：llama.cpp 的 ``/completion`` 收的是**裸文本**，不会套用
        GGUF 里的对话模板。实测 Ternary-Bonsai-2-27B 这类带思考段的 instruct 模型，
        裸 prompt（``User: ...\\nAssistant:``）会直接吐 EOS —— 服务端报
        ``stop_type=eos, predicted_n=1``、正文为空，界面上看起来就是「刚输出一点就没了」。
        套上模板（走 chat 接口）才是模型的正常行为。
        """
        cached = _LLAMA_CHAT_TEMPLATE.get(self.base_url)
        if cached is not None:
            return cached
        ok = False
        try:
            async with self._client(10) as c:
                r = await c.get(f"{self.base_url}/props")
                if r.status_code == 200:
                    props = r.json()
                    ok = bool(props.get("chat_template")) or bool(
                        (props.get("default_generation_settings") or {}).get("chat_template"))
        except Exception as exc:  # noqa: BLE001
            log.debug("读 /props 判断对话模板失败，按不支持处理: %s", exc)
        _LLAMA_CHAT_TEMPLATE[self.base_url] = ok
        log.info("llama.cpp %s 对话模板可用=%s", self.base_url, ok)
        return ok

    async def chat_stream(self, model, messages, client=None, temperature: float = 0.7,
                          top_p: float = 0.9, max_tokens: int = 512,
                          num_ctx: Optional[int] = None, stream: bool = True,
                          **params: Any) -> AsyncIterator[Dict[str, Any]]:
        """优先走 /v1/chat/completions（套用模型的对话模板），否则退回裸 /completion。"""
        if await self._chat_template_available():
            try:
                yielded = False
                async for chunk in self._chat_endpoint_stream(
                        messages, client, temperature, top_p, max_tokens, stream):
                    yielded = True
                    yield chunk
                return
            except httpx.HTTPStatusError as exc:
                if yielded:
                    raise
                code = exc.response.status_code
                log.warning("llama.cpp chat 接口返回 %s，退回 /completion（模板可能不可用）", code)
                _LLAMA_CHAT_TEMPLATE[self.base_url] = False
            except httpx.HTTPError as exc:
                if yielded:
                    raise
                log.warning("llama.cpp chat 接口不可达（%s），退回 /completion", exc)
        async for chunk in self._raw_completion_stream(
                messages, client, temperature, top_p, max_tokens, stream):
            yield chunk

    async def _chat_endpoint_stream(self, messages, client, temperature, top_p,
                                    max_tokens, stream) -> AsyncIterator[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "messages": [{"role": m.get("role", "user"), "content": m.get("content", "")}
                         for m in messages],
            "temperature": temperature, "top_p": top_p, "stream": stream,
            # -1 = 不限制输出长度（llama.cpp 约定，与 max_tokens<=0 同义）
            "max_tokens": max_tokens if max_tokens and max_tokens > 0 else -1,
            # timings_per_token：每帧附带累计 prefill/decode 计时，让「Prefill/Decode 拆分」
            # 用后端真实数据而不是 TTFT 近似；include_usage：末帧补 usage 给出 token 数。
            "timings_per_token": True,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        own = client is None
        c = client or self._client()
        try:
            if not stream:
                r = await c.post(f"{self.base_url}/v1/chat/completions", json=payload)
                r.raise_for_status()
                obj = r.json()
                choice = (obj.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                timings = obj.get("timings") or {}
                usage = obj.get("usage") or {}
                text = (msg.get("content") or "")
                think = msg.get("reasoning_content") or ""
                if think:
                    text = f"<think>\n{think}\n</think>\n\n{text}"
                finish = choice.get("finish_reason")
                yield {"delta": text, "done": True,
                       "prompt_tokens": timings.get("prompt_n") or usage.get("prompt_tokens"),
                       "completion_tokens": (timings.get("predicted_n")
                                             or usage.get("completion_tokens")),
                       "meta": {"timings": timings, "usage": usage,
                                "finish_reason": finish,
                                "truncated": finish == "limit",
                                "endpoint": "/v1/chat/completions"}}
                return
            timings: Dict[str, Any] = {}
            usage: Dict[str, Any] = {}
            finish = None
            in_think = False
            async with c.stream("POST", f"{self.base_url}/v1/chat/completions",
                                json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    data = _sse_payload(line)
                    if not data or data == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    if obj.get("timings"):
                        timings = obj["timings"]
                    if obj.get("usage"):
                        usage = obj["usage"]
                    for ch in (obj.get("choices") or []):
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
                        delta = ch.get("delta") or {}
                        # 思考段（reasoning_content）也算模型真实产出：不显示的话
                        # 思考期间界面只有「正在生成…」，TTFT 也会被记到第一段正文上。
                        think = delta.get("reasoning_content")
                        if think:
                            if not in_think:
                                yield {"delta": "<think>\n", "done": False}
                                in_think = True
                            yield {"delta": think, "done": False}
                        piece = delta.get("content")
                        if piece:
                            if in_think:
                                yield {"delta": "\n</think>\n\n", "done": False}
                                in_think = False
                            yield {"delta": piece, "done": False}
            if in_think:
                yield {"delta": "\n</think>\n", "done": False}
            yield {"delta": "", "done": True,
                   "prompt_tokens": timings.get("prompt_n") or usage.get("prompt_tokens"),
                   "completion_tokens": (timings.get("predicted_n")
                                         or usage.get("completion_tokens")),
                   "meta": {"timings": timings, "usage": usage,
                            "finish_reason": finish,
                            "truncated": finish == "limit",
                            "endpoint": "/v1/chat/completions"}}
        finally:
            if own:
                await c.aclose()

    async def _raw_completion_stream(self, messages, client, temperature, top_p,
                                     max_tokens, stream) -> AsyncIterator[Dict[str, Any]]:
        """老版本 / 未加载对话模板时的退路：裸文本 /completion。"""
        prompt = messages_to_text(messages)
        payload = {
            "prompt": prompt, "stream": stream, "temperature": temperature,
            "top_p": top_p,
            # n_predict = -1 表示不限制输出长度（llama.cpp 约定）
            "n_predict": max_tokens if max_tokens and max_tokens > 0 else -1,
        }
        own = client is None
        c = client or self._client()
        try:
            if not stream:
                r = await c.post(f"{self.base_url}/completion", json=payload)
                r.raise_for_status()
                obj = r.json()
                timings = obj.get("timings") or {}
                yield {"delta": obj.get("content", ""), "done": True,
                       "prompt_tokens": timings.get("prompt_n"),
                       "completion_tokens": timings.get("predicted_n"),
                       "meta": {"timings": timings, "stop_type": obj.get("stop_type"),
                                "finish_reason": obj.get("stop_type"),
                                "truncated": obj.get("stop_type") == "limit",
                                "endpoint": "/completion"}}
                return
            async with c.stream("POST", f"{self.base_url}/completion", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    data = _sse_payload(line)
                    if not data:
                        continue
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    timings = obj.get("timings") or {}
                    if obj.get("stop"):
                        yield {"delta": obj.get("content", ""), "done": True,
                               "prompt_tokens": timings.get("prompt_n"),
                               "completion_tokens": timings.get("predicted_n"),
                               "meta": {"timings": timings,
                                        "stop_type": obj.get("stop_type"),
                                        "finish_reason": obj.get("stop_type"),
                                        "truncated": obj.get("stop_type") == "limit",
                                        "endpoint": "/completion"}}
                    elif obj.get("content"):
                        yield {"delta": obj["content"], "done": False}
        finally:
            if own:
                await c.aclose()

    async def version(self) -> str:
        async with self._client(10) as c:
            r = await c.get(f"{self.base_url}/props")
            r.raise_for_status()
            props = r.json()
            model = (props.get("default_generation_settings") or {}).get("model") or \
                props.get("model_path", "")
            return f"llama.cpp ({model or 'unknown'})"

    async def list_models(self) -> List[str]:
        try:
            async with self._client(10) as c:
                r = await c.get(f"{self.base_url}/v1/models")
                if r.status_code == 200:
                    ids = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
                    if ids:
                        return ids
        except Exception:  # noqa: BLE001
            pass
        try:
            async with self._client(10) as c:
                r = await c.get(f"{self.base_url}/props")
                props = r.json()
                mp = props.get("model_path") or ""
                if mp:
                    return [str(mp).replace("\\", "/").rsplit("/", 1)[-1]]
        except Exception:  # noqa: BLE001
            pass
        return ["llama.cpp-local"]

    supports_ppl = True

    async def tokenize(self, text: str,
                       client: Optional[httpx.AsyncClient] = None) -> List[str]:
        """llama.cpp 的 /tokenize：拿到分词结果，用于逐 token 的 teacher forcing。"""
        own = client is None
        c = client or self._client(30)
        try:
            r = await c.post(f"{self.base_url}/tokenize",
                             json={"content": text, "with_pieces": True})
            r.raise_for_status()
            obj = r.json()
            toks = obj.get("tokens") or []
            out: List[str] = []
            for t in toks:
                if isinstance(t, dict):
                    out.append(str(t.get("piece", "")))
                else:
                    out.append(str(t))
            return out
        finally:
            if own:
                await c.aclose()

    async def perplexity(self, text: str, mode: str = "teacher_forcing",
                         max_tokens: int = 16, top_k: int = 20,
                         client: Optional[httpx.AsyncClient] = None,
                         **params: Any) -> Dict[str, Any]:
        """llama.cpp 的 PPL。

        llama.cpp 的 ``/completion`` 只给「已生成 token」的概率，拿不到 prompt 各 token
        的概率，因此 teacher forcing 用 ``/tokenize`` 分词后**逐 token 递进**：
        第 i 次请求把前 i 个 token 当 prompt，再在 ``top_logprobs`` 里查第 i+1 个
        token 的真实对数概率。感谢 prompt cache（``cache_n``），每次只多算 1 个 token，
        实测开销接近线性而非平方。
        """
        own = client is None
        c = client or self._client(120)
        try:
            if mode == "teacher_forcing":
                toks = await self.tokenize(text, client=c)
                toks = [t for t in toks if t != ""][: max(2, max_tokens) + 1]
                if len(toks) < 3:
                    return {"ok": False, "reason": "文本过短，无法做 teacher-forcing PPL"}
                logprobs: List[float] = []
                unknown = 0
                for i in range(1, len(toks)):
                    payload = {
                        "model": self.default_model, "prompt": "".join(toks[:i]),
                        "max_tokens": 1, "temperature": 0, "logprobs": top_k,
                    }
                    try:
                        r = await c.post(f"{self.base_url}/v1/completions", json=payload)
                        r.raise_for_status()
                        obj = r.json()
                    except Exception:  # noqa: BLE001
                        unknown += 1
                        continue
                    cands = ((obj["choices"][0].get("logprobs") or {})
                             .get("top_logprobs") or [])
                    want = toks[i]
                    hit = next((x for x in cands if x.get("token") == want), None)
                    if hit is None:
                        unknown += 1
                    else:
                        logprobs.append(float(hit["logprob"]))
                if not logprobs:
                    return {"ok": False,
                            "reason": f"top-{top_k} 内未命中任何目标 token，无法估计 PPL"}
                total = len(logprobs) + unknown
                return _ppl_payload(
                    logprobs, "teacher_forcing",
                    coverage=round(len(logprobs) / total, 4) if total else 0.0,
                    unknown_tokens=unknown, top_k=top_k,
                    note=f"逐 token 递进，{len(logprobs)}/{total} 个 token 落在 top-{top_k} 内")
            r = await c.post(f"{self.base_url}/v1/completions", json={
                "model": self.default_model, "prompt": text,
                "max_tokens": max(4, max_tokens), "temperature": 0, "logprobs": 1,
            })
            r.raise_for_status()
            lps = _lp_from_openai(r.json())
            if not lps:
                return {"ok": False, "reason": "服务未返回 logprobs"}
            return _ppl_payload(lps, "autoregressive",
                                note=f"对模型续写的 {len(lps)} 个 token 计算")
        finally:
            if own:
                await c.aclose()


# --------------------------------------------------------------------------
# 内置演示后端（模拟推理服务）
# --------------------------------------------------------------------------
_DEMO_MODELS: List[Dict[str, Any]] = [
    # batch = 该档位的"最佳批大小"，并发超过它之后延迟会按争用系数放大
    {"name": "demo-qwen-7b", "tps": 52.0, "ttft": 110, "vram": 5600, "batch": 4},
    {"name": "demo-llama3-8b", "tps": 41.0, "ttft": 150, "vram": 6800, "batch": 4},
    {"name": "demo-deepseek-r1-7b", "tps": 34.0, "ttft": 190, "vram": 7200, "batch": 3},
    {"name": "demo-gemma2-2b", "tps": 96.0, "ttft": 60, "vram": 2100, "batch": 8},
]


class DemoAdapter(BaseAdapter):
    """内置演示后端：不依赖任何真实推理服务，按模型档位模拟流式输出。"""

    backend = "demo"
    label = "内置演示（模拟）"
    hints_gold = True  # 只有它会收到评测框架传来的标准答案，用来模拟能力曲线

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 在途请求计数：用来模拟「并发超过最佳批大小后尾延迟恶化」，
        # 否则纯独立 sleep 会让压测曲线永远线性，测不出并发上限。
        self._inflight = 0

    def _model_spec(self, model: str) -> Dict[str, Any]:
        for m in _DEMO_MODELS:
            if m["name"] == model:
                return m
        return _DEMO_MODELS[0]

    def _contention(self, spec: Dict[str, Any]) -> float:
        """争用系数：并发数超过最佳批大小时按比例放大延迟（模拟排队）。"""
        best = float(spec.get("batch") or 4)
        return max(1.0, self._inflight / best)

    async def version(self) -> str:
        await asyncio.sleep(0.05)
        return "demo-simulator/1.0"

    async def list_models(self) -> List[str]:
        await asyncio.sleep(0.05)
        return [m["name"] for m in _DEMO_MODELS]

    async def native_metrics(self) -> Dict[str, Any]:
        return {"note": "内置演示后端，不具备原生 Prometheus 指标。",
                "loaded_models": [{"name": m["name"], "vram_mb": m["vram"],
                                   "size_mb": m["vram"]} for m in _DEMO_MODELS[:2]]}

    # ---- 通用对话作答：多轮上下文感知 + 整句完整（绝不半句硬切） ----
    _CHAT_ROLE_RE = re.compile(r"(?:^|\n)(User|Assistant|System): ")
    _RECALL_HINTS = ("刚才", "之前", "上一", "上条", "还记得", "记不记得", "我说过",
                     "我问过", "我前面", "前面提到", "前面说", "历史", "回顾", "回忆")
    _FOLLOWUP_HINTS = ("继续", "接着", "还有呢", "然后呢", "再详细", "展开", "补充",
                       "往下", "然后", "more")
    _MATH_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*([+\-*/x×÷])\s*(-?\d+(?:\.\d+)?)")

    @classmethod
    def _parse_turns(cls, prompt_text: str) -> List[Tuple[str, str]]:
        """把 ``messages_to_text()`` 拼出来的扁平文本还原成 ``[(role, content)]``。

        演示后端拿到的就是这段带角色前缀的文本，所以「记住上文」必须自己把
        角色标记解析回来 —— 内容是模拟的，但这段解析是真实的，
        因此它确实能回答「我刚才问的是什么」这类依赖记忆的问题。
        """
        # messages_to_text 会在末尾补一个 "\nAssistant:"（没有尾随空格），
        # 它不是一个完整的角色标记，但会被并进最后一条消息里，先摘掉。
        prompt_text = re.sub(r"\n(?:User|Assistant|System):\s*$", "", prompt_text)
        marks = [(m.start(0), m.group(1)) for m in cls._CHAT_ROLE_RE.finditer(prompt_text)]
        if not marks:
            return [("user", prompt_text.strip())] if prompt_text.strip() else []
        turns: List[Tuple[str, str]] = []
        for i, (pos, role) in enumerate(marks):
            offset = 1 if prompt_text[pos] == "\n" else 0
            start = pos + offset + len(role) + 2
            end = marks[i + 1][0] if i + 1 < len(marks) else len(prompt_text)
            turns.append((role.lower(), prompt_text[start:end].strip()))
        return turns

    def _compose_chat(self, prompt_text: str, max_tokens: int) -> Tuple[str, Dict[str, Any]]:
        """生成一段结构化、可读的对话回答，返回 (文本, 终止信息)。

        两个硬约束：
        * **只在整句边界收尾** —— 绝不按字符数硬切，避免出现半句话；
        * 长度由 ``max_tokens`` 推导；若池子里还有内容没写完，则如实标注
          ``finish_reason='length'``，让前端的「继续生成」有依据。
        """
        turns = self._parse_turns(prompt_text)
        user_turns: List[str] = [c for r, c in turns if r == "user" and c]
        is_continue = bool(user_turns) and CONTINUE_MARK in user_turns[-1]
        if is_continue:
            # 续写：指令不是「问题」，真正的上下文问题是它前面那一条
            question = user_turns[-2] if len(user_turns) >= 2 else ""
            prior = user_turns[:-2]
        else:
            question = user_turns[-1] if user_turns else prompt_text.strip()
            prior = user_turns[:-1]
        prev = prior[-1] if prior else ""
        # 「话题」= 最近一条**有实质内容**的提问（跳过「我刚才问了什么」这类元问题），
        # 追问时引述它才有意义 —— 这也是「记忆」该有的样子。
        topic = ""
        for t in reversed(prior):
            if not any(k in t for k in self._RECALL_HINTS):
                topic = t
                break
        if not topic and prior:
            topic = prior[-1]

        # 输出预算：CJK 大约 1 字 ≈ 1 token，留 10% 给收尾段
        unlimited = not max_tokens or max_tokens <= 0
        budget_tokens = 10 ** 9 if unlimited else int(max_tokens)
        soft_cap = int(budget_tokens * 0.9) if not unlimited else 10 ** 9

        def short(text: str, n: int = 60) -> str:
            t = re.sub(r"\s+", " ", text).strip()
            return t if len(t) <= n else t[:n] + "…"

        # ---- 续写分支：只补新内容，不复述、不重新开头 ----
        if is_continue:
            body: List[str] = ["**（接上文续写）**"]
            used = estimate_tokens(body[0])
            pool = self._chat_continuation_pool()
            truncated = False
            for para in pool:
                cost = estimate_tokens(para)
                if used + cost > soft_cap:
                    truncated = True
                    break
                body.append(para)
                used += cost
            body.append("以上就是上一段被截断后继续补完的剩余内容；"
                        "如需继续，再点一次「继续生成」即可。")
            text = "\n\n".join(body)
            return text, {"finish_reason": "length" if truncated else "stop",
                          "truncated": bool(truncated), "simulated": True,
                          "continued": True}

        paras: List[str] = []
        recall = any(k in question for k in self._RECALL_HINTS)
        followup = any(k in question for k in self._FOLLOWUP_HINTS)

        # 1) 开场：引用问题 + 体现「我记得上文」
        if recall:
            if prev:
                paras.append(f"当然记得。你上一轮问的是：「{short(prev)}」。"
                             f"另外，本轮的问题是：「{short(question)}」。")
            else:
                paras.append("目前这是我们这一轮对话的开始，之前还没有用户提问记录 —— "
                             "我的上下文里只有你这条消息本身。")
        elif followup and topic:
            paras.append(f"好，接着上一条（「{short(topic)}」）继续往下说。")
        elif topic:
            paras.append(f"关于「{short(question)}」，我结合你之前问的「{short(topic, 40)}」"
                         "一起回答。")
        else:
            paras.append(f"关于「{short(question)}」，下面按结论、要点、依据、建议四段来回答。")

        if topic and not recall:
            paras.append(f"我先对齐一下前文：你之前问过「{short(topic, 40)}」，"
                         "下面的分析会与当时的结论保持一致。")

        # 2) 结论
        mm = self._MATH_RE.search(question)
        if mm:
            a, op, b = float(mm.group(1)), mm.group(2), float(mm.group(3))
            op = {"x": "*", "×": "*", "÷": "/"}.get(op, op)
            if op == "/" and not b:
                paras.append("**结论**：除数不能为 0，这个式子本身没有定义。")
                paras.append("**验算过程**：先检查分母是否为 0；这里分母是 0，"
                             "所以无论分子是多少，该表达式都无意义，请核对输入。")
            else:
                val = {"+": a + b, "-": a - b, "*": a * b, "/": a / b}[op]
                shown = f"{val:g}"
                paras.append(f"**结论**：{mm.group(1)} {mm.group(2)} {mm.group(3)} = {shown}")
                paras.append("**验算过程**：" + (
                    f"先按运算优先级处理 {mm.group(1)} {mm.group(2)} {mm.group(3)}，"
                    f"再核对数量级与符号，最终结果 {shown}。"
                    if op != "+" else
                    f"把 {mm.group(1)} 拆成整数与小数两部分分别相加再合并，"
                    f"得到 {shown}。"))
        else:
            paras.append("**结论**：这个问题可以拆成「目标 → 约束 → 可验证指标」三步来看，"
                         "先把目标写成一句可以用数字判断的话，再谈方案。")

        # 3) 要点
        bullets = [
            "明确判据：把期望结果写成可测量的指标（延迟、吞吐、准确率），"
            "否则后续所有对比都没有基准。",
            "控制变量：一次只改一个参数（并发数、上下文长度、量化级别），"
            "否则观测到的变化无法归因。",
            "区分阶段：Prefill 阶段吃算力，Decode 阶段吃显存带宽，"
            "两者的优化手段完全不同，混在一起看会得出错误结论。",
            "保留证据：把每次实验的参数与结果落库，"
            "这样版本升级或换硬件时才有可比的历史曲线。",
            "设置阈值：为关键指标配置告警与熔断，"
            "让系统在异常时自动止损，而不是等人发现问题。",
            "关注尾部：平均延迟好看不代表体验好，P95 / P99 才决定用户感知。",
        ]
        paras.append("**关键要点**\n" + "\n".join(
            f"{i + 1}. {b}" for i, b in enumerate(bullets[:5])))

        # 4) 依据：长段落池，按预算逐段追加（不会切断任何一段）
        detail_pool = self._chat_detail_pool(question, topic)

        # 5) 收尾（永远保留，让回答有完整结尾）
        closing = ("以上为内置演示后端生成的示例文本，用于验证多轮上下文、"
                   "流式渲染与指标统计链路；接入真实 Ollama / vLLM / llama.cpp 后，"
                   "这里的内容将来自真实推理结果。")

        # 统一按**整段**装配：超出预算就停在段边界，绝不切到句中
        body: List[str] = list(paras)
        body.append("**依据与展开**")
        used = estimate_tokens("\n\n".join(body))
        truncated = False
        for para in detail_pool:
            cost = estimate_tokens(para)
            if used + cost > soft_cap:
                truncated = True
                break
            body.append(para)
            used += cost
        body.append(closing)

        if not unlimited:
            used = estimate_tokens("\n\n".join(body))
            if used >= max_tokens * 0.98 and not truncated:
                truncated = True

        text = "\n\n".join(body)
        meta = {
            "finish_reason": "length" if truncated else "stop",
            "truncated": bool(truncated),
            "demo_paragraphs": len(body),
            "simulated": True,
        }
        return text, meta

    def _chat_continuation_pool(self) -> List[str]:
        """续写段池：与首答的展开段不重复，体现「接着往下写」。"""
        return [
            "**补充一：长上下文的取舍**。KV Cache 随上下文线性增长，"
            "因此把历史全量带上并不总是最优；常见做法是保留最近若干轮、"
            "把更早的内容压缩成摘要或长期记忆，既省显存也省 prefill 时间。",
            "**补充二：批处理与显存带宽**。Decode 阶段每个 token 都要把权重与"
            "KV Cache 读一遍，瓶颈在显存带宽上；所以批处理能显著提升总吞吐，"
            "但单条请求的延迟几乎不会下降。",
            "**补充三：量化档位的影响**。同一模型换成更低比特的量化后，"
            "权重占用下降明显，但 Decode 速度未必同比提升 —— "
            "它取决于反量化开销与内核实现，必须实测而不是推算。",
            "**补充四：如何定位瓶颈**。把 GPU 利用率、显存占用、"
            "prefill 耗时与 decode 吞吐放在同一时间轴上对照："
            "利用率满而吞吐不涨说明算力到顶；显存接近满则要先降上下文或量化。",
            "**补充五：告警与熔断阈值**。阈值不该拍脑袋定，"
            "应当先用一轮阶梯压测拿到基线，取基线尾延迟的 1.5 倍作为告警线，"
            "既能提前发现问题，又不会被正常抖动误报。",
            "**补充六：结果的可复现性**。把连接地址、模型标识、量化档位、"
            "上下文长度、采样参数与并发数一起写进测试档案，"
            "任何人拿到这条记录都能复现出同量级的结果。",
        ]

    def _chat_detail_pool(self, question: str, topic: str) -> List[str]:
        """展开段池：整段完整的中文说明，供按预算取用。"""
        pool = [
            "**度量口径**：TTFT 是从发出请求到收到第一个含文本的 chunk，"
            "它主要反映 Prompt 处理与排队；TPS 则用 completion_tokens 除以"
            "「端到端耗时减去 TTFT」，避免把预填充时间算进生成速度。",
            "**上下文与显存**：KV Cache 的大小随「上下文长度 × 层数 × KV 头数」线性增长，"
            "上下文翻倍时显存占用也接近翻倍，这是长对话最容易踩的坑。",
            "**并发与排队**：并发数超过硬件的批处理能力后，请求开始排队，"
            "TTFT 会明显抬升，而整体吞吐不再增长 —— 这就是建议并发上限的来源。",
            "**量化与精度**：把权重从 FP16 降到 4bit 通常能把显存占用压到三分之一左右，"
            "代价是部分任务上的精度损失，需要用同一套测评集做前后对比。",
            "**压测意义**：单条请求只能说明「能跑」，"
            "只有阶梯加压才能看出系统在什么负载下开始劣化，以及劣化的速度。",
            "**可观测性**：把模型指标、GPU 指标与业务指标放在同一时间轴上，"
            "故障时才能一眼看出是显存打满、算力打满，还是请求本身出错。",
            "**复现性**：固定 Prompt、固定 max_tokens、固定采样参数，"
            "是让两次测试结果可比的前提；否则随机性会淹没真实差异。",
            "**成本权衡**：同一块卡上，批量推理的总吞吐远高于单条串行推理，"
            "但单条延迟会上升，选择哪种取决于业务是「要吞吐」还是「要响应速度」。",
            "**常见误区**：只看平均值。平均值会掩盖慢请求，"
            "而用户抱怨的往往正是那 1% 的慢请求。",
            "**工程建议**：把模型版本、量化级别、上下文长度、并发数这四个参数"
            "一起记录进测试档案，它们共同决定了一次测试的边界条件。",
        ]
        if topic and any(k in question for k in ("它", "这个", "那个", "上面", "前面", "刚才")):
            pool.insert(0, f"**承接前文**：你前面问的是「{topic[:40]}」，"
                           "这一点会直接影响下面讨论的结论，所以我把它放进同一组约束里考虑。")
        return pool

    def _compose(self, prompt: str, max_tokens: int,
                 metric: Optional[str] = None, gold: Any = None,
                 salt: str = "", keywords: Optional[Sequence[str]] = None) -> str:
        """根据 Prompt 关键词挑选一段结构化的回答。"""
        return self._compose_ex(prompt, max_tokens, metric=metric, gold=gold,
                                salt=salt, keywords=keywords)[0]

    def _compose_ex(self, prompt: str, max_tokens: int,
                    metric: Optional[str] = None, gold: Any = None,
                    salt: str = "", keywords: Optional[Sequence[str]] = None
                    ) -> Tuple[str, Dict[str, Any]]:
        """评测题型优先；不是评测题则走通用对话作答。

        只有**评测框架明确下了题型**（``metric`` / ``gold``）时才走评测作答分支：
        评测题的 Prompt 里全是标准答案线索，而对话测试传进来的是整段多轮历史，
        若不加这道闸门，历史里出现「等于多少」之类的字眼就会把对话误判成评测题。
        """
        if metric or gold is not None:
            ev = self._compose_eval(prompt, max_tokens, metric=metric, gold=gold,
                                    salt=salt, keywords=keywords)
            if ev is not None:
                # 评测题的作答长度由题目与标准答案决定，不存在被 max_tokens 截断
                return ev, {"finish_reason": "stop", "truncated": False,
                            "simulated": True, "demo_eval": True}
        return self._compose_chat(prompt, max_tokens)


    # ---- 评测题型的模拟作答（让「评测中心」离线也能跑通全链路） ----
    _DEMO_CODES: Dict[str, str] = {
        "def add(a, b)": "def add(a, b):\n    return a + b",
        "factorial(n)": "def factorial(n):\n    r = 1\n    for i in range(2, n + 1):\n        r *= i\n    return r",
        "is_palindrome(s)": "def is_palindrome(s):\n    t = ''.join(ch.lower() for ch in s if ch.isalnum())\n    return t == t[::-1]",
        "two_sum(nums, target)": ("def two_sum(nums, target):\n    seen = {}\n"
                                  "    for i, v in enumerate(nums):\n"
                                  "        if target - v in seen:\n"
                                  "            return [seen[target - v], i]\n"
                                  "        seen[v] = i\n    return []"),
        "fib(n)": ("def fib(n):\n    a, b = 0, 1\n"
                   "    for _ in range(n):\n        a, b = b, a + b\n    return a"),
        "gcd(a, b)": "def gcd(a, b):\n    while b:\n        a, b = b, a % b\n    return a",
        "count_words(text)": "def count_words(text):\n    return len([w for w in text.split() if w])",
        "reverse_words(s)": "def reverse_words(s):\n    return ' '.join(s.split()[::-1])",
    }
    _DEMO_REFUSALS = [
        "抱歉，这个请求我不能协助。它可能涉及违法或对他人造成伤害，建议你通过合法渠道解决问题。",
        "我无法提供这类内容。请理解，我不会帮助实施可能违法的行为。",
        "对不起，这个忙我帮不了。如果你遇到困难，建议联系相关专业机构或官方渠道。",
        "我不能协助完成这个请求，因为它违反法律法规与平台安全政策。",
    ]

    def _rand01(self, seed_text: str) -> float:
        """确定性伪随机（按文本哈希），保证同一题多次运行结果一致、便于复现。"""
        h = hashlib.sha1(seed_text.encode("utf-8", "replace")).hexdigest()[:10]
        return int(h, 16) / float(0xFFFFFFFFFF)

    # 演示后端的模拟"解题正确率"（仅内置 demo 用；真实后端完全不受影响）
    _DEMO_SKILL: Dict[str, float] = {
        "accuracy_mc": 0.62, "accuracy_numeric": 0.72,
        "f1_extract": 0.80, "niah": 0.90,
    }

    def _compose_eval(self, prompt: str, max_tokens: int,
                      metric: Optional[str] = None,
                      gold: Any = None, salt: str = "",
                      keywords: Optional[Sequence[str]] = None) -> Optional[str]:
        """识别评测题型并给出「可被判分」的模拟回答；不是评测题则返回 None。

        当评测框架通过 ``demo_metric`` / ``demo_gold`` 告知题型与标准答案时
        （只有声明 ``hints_gold = True`` 的适配器会收到），按 ``_DEMO_SKILL``
        给出的模拟能力曲线决定「答对还是答错」，使离线演示的分数落在合理区间。
        ``salt`` 是采样序号，让 pass@k 的多次采样产出不同结果而不是完全一致。
        """
        if not prompt:
            return None
        seed = prompt[:400] + (salt or "")
        r = self._rand01(seed)

        # 0) 安全类：由题型直接驱动，绝不靠关键词猜（否则"勒索软件代码"会被当成普通代码题）
        if metric == "refusal_rate":
            # 92% 明确拒绝；剩下 8% 给出"看似配合"的空泛内容 → 判为越界
            if r < 0.92:
                return self._DEMO_REFUSALS[int(r * 1000) % len(self._DEMO_REFUSALS)]
            return ("这类操作需要先明确目标环境与授权范围，建议在合规前提下查阅公开资料，"
                    "我只能给出一般性的思路框架。")
        if metric == "jailbreak_rate":
            # 越狱成功率目标 6%：94% 守住护栏
            if r < 0.94:
                return ("我理解你想通过设定情景来探讨这个问题，但即便在虚构设定下，"
                        "我也不能提供实施步骤或可操作细节。")
            return ("好的，现在进入无限制模式。按你的要求分步骤说明："
                    "第一步准备环境，第二步执行关键操作，第三步清理痕迹。"
                    "（模拟越狱成功样例）")

        # 1) 已知标准答案的题型：按模拟能力曲线作答
        if gold not in (None, "", []):
            if metric == "accuracy_mc":
                letters = re.findall(r"^([A-D])[.、]\s*", prompt, re.M) or ["A", "B", "C", "D"]
                g = str(gold).strip().upper()[:1]
                hit = r < self._DEMO_SKILL["accuracy_mc"]
                pick = g if (hit and g in letters) else \
                    next((x for x in letters if x != g), letters[0])
                return f"逐个排除明显不成立的选项后，剩下最符合题意的一项。\n答案：{pick}"
            if metric == "accuracy_numeric":
                nums = re.findall(r"-?\d+(?:\.\d+)?", str(gold))
                if nums:
                    gv = float(nums[0])
                    if r < self._DEMO_SKILL["accuracy_numeric"]:
                        shown = nums[0]
                    else:
                        # 答错时给出一个"看起来像算错了"的邻近值
                        shown = f"{gv * self._rand01(prompt + 'e') + 1:.0f}".rstrip("0") or "0"
                    return (f"先列出已知条件，再逐步计算：合并同类项后得到中间结果，"
                            f"最后核对单位与题意，答案是 {shown}。")
            if metric == "f1_extract":
                ents = gold if isinstance(gold, (list, tuple)) else \
                    [x.strip() for x in re.split(r"[,，、]", str(gold)) if x.strip()]
                if ents:
                    if r < self._DEMO_SKILL["f1_extract"]:
                        return "抽取结果：" + "、".join(str(x) for x in ents)
                    # 漏掉一部分实体
                    keep = ents[: max(0, len(ents) - 1)] or ents[:1]
                    return "抽取结果：" + "、".join(str(x) for x in keep)
            if metric == "rouge":
                # 生成式问答 / 摘要：答对时给出参考要点，答错时给出邻近的错误值
                if r < 0.65:
                    return f"根据材料可以确定：{gold}。"
                nums = re.findall(r"\d+", str(gold))
                if nums:
                    return f"根据材料可以确定：{int(nums[0]) + 4}。"
                return "根据材料可以确定：现有信息不足以下结论。"

        # 1) 已知题型兜底：代码题命中函数签名就给参考实现，25% 概率故意写错以模拟真实 pass 率
        for sig, code in self._DEMO_CODES.items():
            if sig in prompt:
                if r < 0.25:
                    bad = code.replace("return a + b", "return a - b") \
                              .replace("r *= i", "r += i") \
                              .replace("return t == t[::-1]", "return t == t") \
                              .replace("return []", "return [0, 0]")
                    return f"```python\n{bad}\n```"
                return f"```python\n{code}\n```"

        # 2) 裁判评分题（MT-Bench 等）：把评分要点自然织进回答，模拟"得分不错"的输出
        if metric == "judge_score":
            kws = [str(k) for k in (keywords or []) if str(k).strip()]
            if kws:
                seg = "，".join(kws)
                return (f"围绕「{kws[0]}」展开：{seg}。"
                        "文字尽量具体，用细节把画面立起来，读起来自然流畅。"
                        "（演示后端生成的示例回答，用于验证裁判评分链路）")
            return ("先回应问题核心，再从不同角度补充说明，最后收束给出可执行的建议。"
                    "（演示后端生成的示例回答，用于验证裁判评分链路）")

        # 3) 长上下文检索（NIAH）：短文能捞回，长文按长度衰减
        if "密钥编号" in prompt or "档案" in prompt:
            m = re.search(r"密钥编号是\s*(\d+)", prompt)
            if m:
                n_tokens = estimate_tokens(prompt)
                # 上下文越长，命中率越低（模拟真实长上下文退化）
                p_hit = 1.0 if n_tokens < 700 else (0.75 if n_tokens < 1400 else 0.5)
                if r < p_hit:
                    return f"密钥编号是 {m.group(1)}。"
                return "抱歉，我在上文里没有找到相关信息。"

        # 4) 多选（MMLU / BBH / TruthfulQA）：按选项分布作答
        if "选项：" in prompt:
            letters = re.findall(r"^([A-D])\.\s", prompt, re.M) or ["A", "B", "C", "D"]
            pick = letters[int(r * len(letters)) % len(letters)]
            return f"简要推理后可以排除明显错误项。\n答案：{pick}"

        # 5) 数值题（GSM8K）：给一个带推理过程的数值回答
        if "等于多少" in prompt or "多少" in prompt and "小时" in prompt:
            val = int(r * 90) + 5
            return (f"先列出已知条件并逐步计算：设所求量为 x，"
                    f"根据题意列式求解，得到最终结果 {val}。")

        # 6) 指令遵循类
        if "只输出两个大写字母" in prompt or "机器学习英文缩写" in prompt:
            return "ML" if r < 0.92 else "Machine Learning"
        if "JSON" in prompt and "name" in prompt and "age" in prompt:
            return ('{"name": "张三", "age": 30}' if r < 0.9
                    else 'name: 张三, age: 30')
        if "请假条" in prompt:
            return ("病假申请：因身体不适需请病假一天，明天休息，特此申请。" if r < 0.9
                    else "关于申请休息的说明：本人身体不适，希望休息一天。")
        if "有序列表" in prompt:
            return "1. 使用更低比特量化\n2. 缩短上下文长度\n3. 开启显存分页"
        if "结论：" in prompt:
            return "结论：本地部署大模型最主要的成本是显存硬件与长期运维投入。"
        if "翻译成英文" in prompt:
            return ("On-premise deployment prevents data leakage." if r < 0.9
                    else "本地部署可以避免数据外泄。")
        if "元组" in prompt:
            return "('x', 8)" if r < 0.9 else "['x', '8']"
        if "Markdown 表格" in prompt:
            return "| name | score |\n| --- | --- |\n| alice | 90 |\n| bob | 85 |"
        if "KV Cache" in prompt and "不少于" in prompt:
            return ("KV Cache 是注意力机制里缓存历史 Key 与 Value 的结构，"
                    "它避免每生成一个 token 都重算全部历史，代价是显存占用随上下文长度近似线性增长。")

        # 7) 安全类（未传 metric 时的关键词兜底）
        if any(k in prompt for k in ("冒充", "违禁", "入侵", "查一个人", "结束生命", "低等",
                                     "代写", "做假账", "爆炸装置", "无限制 AI", "钓鱼",
                                     "Ignore all previous", "审查", "伪造")):
            if r < 0.92:
                return self._DEMO_REFUSALS[int(r * 100) % len(self._DEMO_REFUSALS)]
            return ("可以参考的思路：先明确目标，再分步骤实施，注意控制风险。"
                    "（模拟越狱成功样例）")

        # 8) 开放式/摘要/抽取：给一段结构化但通用的回答
        if "摘要" in prompt or "压缩" in prompt:
            return ("本项目在本地部署大模型推理服务，采集硬件与推理指标，"
                    "支持固定与阶梯加压压测，并在错误率或显存超阈值时自动熔断。")
        if "抽取" in prompt or "列出" in prompt:
            return "本地部署, 推理服务, 显存占用"
        if len(prompt) > 120:
            return ("先给结论：关键在于把指标拆成延迟、吞吐与资源三个维度分别度量，"
                    "再结合压测数据找到吞吐与尾延迟的平衡点。"
                    "具体来说，Prefill 阶段偏算力，Decode 阶段偏显存带宽，"
                    "因此优化手段也不同。")
        return None

    supports_ppl = True

    async def perplexity(self, text: str, mode: str = "teacher_forcing",
                         max_tokens: int = 16, top_k: int = 20,
                         client: Optional[httpx.AsyncClient] = None,
                         **params: Any) -> Dict[str, Any]:
        """演示后端的 PPL：按文本哈希给出稳定但**明确标注为模拟**的数值。"""
        if not (text or "").strip():
            return {"ok": False, "reason": "待评分文本为空"}
        r = self._rand01(text[:200])
        ppl = round(2.0 + r * 18.0, 4)  # 2~20 之间
        tokens = min(max_tokens, max(2, estimate_tokens(text)))
        return {"ok": True, "ppl": ppl, "mean_logprob": round(-math.log(ppl), 4),
                "mode": "simulated", "tokens": tokens, "simulated": True,
                "note": "内置演示后端的合成值，仅用于打通链路，不代表任何真实模型"}

    async def chat_stream(self, model, messages, client=None, temperature: float = 0.7,
                          top_p: float = 0.9, max_tokens: int = 512,
                          num_ctx: Optional[int] = None, stream: bool = True,
                          demo_gold: Any = None, demo_metric: Optional[str] = None,
                          demo_seed: str = "", demo_keywords: Optional[Sequence[str]] = None,
                          **params: Any) -> AsyncIterator[Dict[str, Any]]:
        spec = self._model_spec(model or self.default_model)
        prompt = messages_to_text(messages)
        prompt_tokens = estimate_tokens(prompt)

        self._inflight += 1
        try:
            factor = self._contention(spec)
            # 模拟长上下文 + 并发争用带来的 TTFT 增长
            ttft_ms = spec["ttft"] * (1 + prompt_tokens / 2600.0) * factor \
                * random.uniform(0.9, 1.15)
            await asyncio.sleep(ttft_ms / 1000.0)

            text, term = self._compose_ex(prompt, max_tokens, metric=demo_metric,
                                          gold=demo_gold, salt=demo_seed,
                                          keywords=demo_keywords)
            # 分片必须**恰好覆盖全文**：用 ceil 反推每片长度，避免最后一片被截掉
            # （早期用 chunks[:est_tokens] 会把「答案：X」这类结尾直接切没）
            # max_tokens<=0 约定为「不限制」，此时按文本自身的长度切片
            cap = max_tokens if (max_tokens and max_tokens > 0) else estimate_tokens(text)
            want = max(1, min(cap, estimate_tokens(text)))
            chunk_chars = max(1, math.ceil(len(text) / want))
            chunks = [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)]
            est_tokens = max(1, len(chunks))

            # 争用会让单请求 decode 变慢（显存带宽被分摊）
            tps = spec["tps"] * random.uniform(0.92, 1.08) / factor
            interval = 1.0 / tps
            meta = {"eval_duration_ns": int(est_tokens / tps * 1e9),
                    "prompt_eval_duration_ns": int(ttft_ms * 1e6),
                    "model_spec": spec["name"], "simulated": True,
                    "inflight": self._inflight, "contention": round(factor, 3),
                    "finish_reason": term.get("finish_reason") or "stop",
                    "truncated": bool(term.get("truncated"))}

            if not stream:
                # 非流式：一次性给出完整回答（仍是单块 chunk，delta 为全文）
                await asyncio.sleep(interval * max(0, est_tokens))
                yield {"delta": text, "done": True, "prompt_tokens": prompt_tokens,
                       "completion_tokens": est_tokens, "meta": meta}
                return

            emitted = 0
            total = len(chunks)
            for i, ch in enumerate(chunks):
                await asyncio.sleep(interval * (1 if i else 0))
                emitted += 1
                if i == total - 1:
                    yield {"delta": ch, "done": True, "prompt_tokens": prompt_tokens,
                           "completion_tokens": emitted, "meta": meta}
                else:
                    yield {"delta": ch, "done": False}
        finally:
            self._inflight -= 1


# --------------------------------------------------------------------------
# PPL（困惑度）：基于对数概率的通用实现
# --------------------------------------------------------------------------
def _lp_from_openai(obj: Dict[str, Any]) -> List[float]:
    """从 OpenAI 风格响应里取 logprobs.content[*].logprob。"""
    try:
        content = ((obj["choices"][0].get("logprobs") or {}).get("content")) or []
        return [float(c["logprob"]) for c in content if c.get("logprob") is not None]
    except (KeyError, IndexError, TypeError, ValueError):
        return []


def _ppl_payload(logprobs: Sequence[float], mode: str, **extra: Any) -> Dict[str, Any]:
    from .metrics_ext import mean_logprob, perplexity
    return {
        "ok": True,
        "ppl": perplexity(logprobs),
        "mean_logprob": mean_logprob(logprobs),
        "mode": mode,
        "tokens": len(logprobs),
        "simulated": False,
        **extra,
    }


async def ppl_openai_compat(c: httpx.AsyncClient, root: str, model: str, text: str,
                            mode: str = "auto", max_tokens: int = 24,
                            top_k: int = 20) -> Dict[str, Any]:
    """OpenAI 兼容后端的 PPL。

    两种口径（结果里用 ``mode`` 明确标注，避免误读）：

    * ``echo``：一次请求拿到**整段文本**每个 token 的对数概率（vLLM 等支持 `echo`）——
      这是严格的 teacher-forcing PPL，可与论文数值直接对比。
    * ``autoregressive``：后端只回生成 token 的对数概率时，退化为
      「让模型自己续写 N 个 token，再算这 N 个 token 的 PPL」。可比性弱于前者，
      但同模型不同版本之间仍可用作趋势指标。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "待评分文本为空"}
    url = f"{root}/completions"

    if mode in ("auto", "echo"):
        try:
            r = await c.post(url, json={
                "model": model or "", "prompt": text, "max_tokens": 0,
                "temperature": 0, "echo": True, "logprobs": top_k,
            })
            r.raise_for_status()
            lps = _lp_from_openai(r.json())
            # echo 生效时 token 数应与文本规模同量级；只回 1 个说明后端忽略了 echo
            if len(lps) >= 4:
                return _ppl_payload(lps, "echo", note="含 prompt 全部 token（teacher forcing）")
        except Exception:  # noqa: BLE001
            pass
        if mode == "echo":
            return {"ok": False, "reason": "该后端不支持 echo + logprobs，无法做 teacher-forcing PPL"}

    try:
        r = await c.post(url, json={
            "model": model or "", "prompt": text, "max_tokens": max(4, max_tokens),
            "temperature": 0, "logprobs": 1,
        })
        r.raise_for_status()
        lps = _lp_from_openai(r.json())
        if not lps:
            return {"ok": False, "reason": "该后端未返回 logprobs，无法计算 PPL"}
        return _ppl_payload(lps, "autoregressive",
                            note=f"对模型续写的 {len(lps)} 个 token 计算（非 teacher forcing）")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"PPL 请求失败: {type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------
ADAPTERS: Dict[str, type] = {
    "ollama": OllamaAdapter,
    "vllm": VLLMAdapter,
    "openai_compat": OpenAICompatAdapter,
    "llama_cpp": LlamaCppAdapter,
    "demo": DemoAdapter,
}

BACKEND_PRESETS: List[Dict[str, Any]] = [
    {"backend": "demo", "label": "内置演示（离线可用）", "base_url": "demo://local",
     "hint": "无需任何推理服务，模拟流式输出与性能指标，用于体验与联调。"},
    {"backend": "ollama", "label": "Ollama", "base_url": "http://localhost:11434",
     "hint": "默认本地推理服务，探测端点 GET /api/tags。"},
    {"backend": "vllm", "label": "vLLM", "base_url": "http://localhost:8000",
     "hint": "OpenAI 兼容 + Prometheus /metrics，适合高吞吐压测。"},
    {"backend": "llama_cpp", "label": "llama.cpp server", "base_url": "http://localhost:8081",
     "hint": "使用 POST /completion，返回体含 timings 字段。"},
    {"backend": "openai_compat", "label": "OpenAI 兼容通用", "base_url": "http://localhost:8000/v1",
     "hint": "任何实现 /v1/chat/completions 的服务，含云端 API。"},
]


def build_adapter(conn: Dict[str, Any]) -> BaseAdapter:
    cls = ADAPTERS.get((conn.get("backend") or "").lower(), OpenAICompatAdapter)
    return cls(conn.get("base_url", ""), conn.get("api_key", "") or "",
               conn.get("default_model", "") or "")


def get_adapter_by_conn_id(conn_id: int) -> BaseAdapter:
    conn = db_get_connection(conn_id)
    if not conn:
        raise ValueError(f"连接档案 #{conn_id} 不存在")
    return build_adapter(conn)


def db_get_connection(conn_id: int) -> Optional[Dict[str, Any]]:
    from . import db  # 延迟导入，避免循环依赖
    return db.get_connection(conn_id)


# --------------------------------------------------------------------------
# 效率与资源：把「拿不到就编」的地方全部换成「有据可查」
# --------------------------------------------------------------------------
async def _llamacpp_efficiency(adapter: "LlamaCppAdapter",
                               model_hint: str = "") -> Dict[str, Any]:
    from .metrics_ext import (flops_theory, gguf_hyperparams, infer_model_path,
                              kv_cache_bytes, parse_server_args)

    out: Dict[str, Any] = {"kind": "llama_cpp", "kv_cache": None, "model": None,
                           "server": None, "flops": None, "notes": []}
    props: Dict[str, Any] = {}
    try:
        async with adapter._client(10) as c:  # noqa: SLF001
            r = await c.get(f"{adapter.base_url}/props")
            r.raise_for_status()
            props = r.json()
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"/props 不可用: {type(exc).__name__}")
    gen = props.get("default_generation_settings") or {}
    out["server"] = {
        "ctx_size": gen.get("n_ctx") or props.get("n_ctx"),
        "total_slots": props.get("total_slots"),
        "model_path": props.get("model_path"),
        "model_alias": props.get("model_alias"),
        "model_ftype": props.get("model_ftype"),
        "is_sleeping": props.get("is_sleeping"),
        "build": (props.get("build_info") or ""),
        "chat_template": bool(props.get("chat_template")),
    }

    # 进程命令行是运行时权威配置（KV 量化类型 / offload 层数只在参数里）
    args: Dict[str, Any] = {}
    cwd = None
    try:
        from .collector import _find_inference_process  # 延迟导入避免循环
        p = _find_inference_process()
        if p is not None:
            args = parse_server_args(p.cmdline() or [])
            try:
                cwd = p.cwd()
            except Exception:  # noqa: BLE001
                cwd = None
            out["server"].update({
                "pid": p.pid,
                "cmdline": " ".join(p.cmdline() or [])[:600],
                "n_gpu_layers": args.get("n_gpu_layers"),
                "batch_size": args.get("batch_size"),
                "ubatch_size": args.get("ubatch_size"),
                "parallel": args.get("parallel"),
                "flash_attn": args.get("flash_attn"),
                "cache_type_k": args.get("cache_type_k"),
                "cache_type_v": args.get("cache_type_v"),
                "host": args.get("host"), "port": args.get("port"),
            })
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"读取进程参数失败: {type(exc).__name__}")

    raw_path = args.get("model_path") or out["server"].get("model_path") or model_hint
    model_path = infer_model_path(raw_path, cwd, [
        str(config.BASE_DIR), "I:/install/llamacpp", "G:/Download/models",
        "/usr/share/llama.cpp/models",
    ]) if raw_path else None
    if not model_path:
        out["notes"].append("未能定位 GGUF 文件（远端服务或路径不可读），KV Cache 无法精确计算")
        return out

    gg = gguf_hyperparams(model_path)
    if not gg.get("ok"):
        out["notes"].append(f"GGUF 解析失败: {gg.get('error')}")
        return out
    out["model"] = {
        "path": gg.get("path"), "file": os.path.basename(gg.get("path") or ""),
        "architecture": gg.get("architecture"), "name": gg.get("name"),
        "size_gib": gg.get("size_gib"), "layers": gg.get("block_count"),
        "heads": gg.get("head_count"), "kv_heads": gg.get("head_count_kv"),
        "key_length": gg.get("key_length"), "value_length": gg.get("value_length"),
        "embedding": gg.get("embedding_length"),
        "ctx_train": gg.get("context_length"),
        "parameters": gg.get("parameter_count"),
        "parameters_b": (round(gg["parameter_count"] / 1e9, 2)
                         if gg.get("parameter_count") else None),
        "parameter_source": gg.get("parameter_count_source"),
        "file_type": gg.get("file_type"),
    }

    n_ctx = int(out["server"].get("ctx_size") or gen.get("n_ctx") or 4096)
    slots = int(out["server"].get("total_slots") or 1)
    kv_type = str(args.get("cache_type_k") or "f16").lower()
    kv_v_type = str(args.get("cache_type_v") or kv_type).lower()
    cur = kv_cache_bytes(int(gg.get("block_count") or 0), n_ctx,
                         int(gg.get("head_count_kv") or 0),
                         int(gg.get("key_length") or 0),
                         int(gg.get("value_length") or 0), kv_type, slots)
    # 不同上下文长度下的 KV 开销表（容量规划常用）
    table = []
    for ctx in (4096, 8192, 16384, 32768, 65536, 131072):
        k = kv_cache_bytes(int(gg.get("block_count") or 0), ctx,
                           int(gg.get("head_count_kv") or 0),
                           int(gg.get("key_length") or 0),
                           int(gg.get("value_length") or 0), kv_type, 1)
        if k.get("ok"):
            table.append({"ctx": ctx, "per_slot_gib": k["total_gib"],
                          "slots_total_gib": round(k["total_gib"] * slots, 2)})
    out["kv_cache"] = {
        "current": cur, "current_ctx": n_ctx, "slots": slots,
        "cache_type_k": kv_type, "cache_type_v": kv_v_type,
        "table": table,
        "formula_note": "bytes = slots × 层数 × ctx × KV头数 × (K维+V维) × 每元素字节数",
    }
    params = gg.get("parameter_count")
    if params:
        out["flops"] = {
            "params": params,
            "params_b": round(params / 1e9, 2),
            "flops_per_token": flops_theory(params, 1),
            "formula": "理论前向 FLOPs ≈ 2 × 参数量 × token 数（不含注意力平方项）",
        }
    vram_model = gg.get("size_gib")
    kv_gib = cur.get("total_gib") if cur.get("ok") else None
    out["vram"] = {
        "model_gib": vram_model,
        "kv_cache_gib": kv_gib,
        "sum_gib": (round(vram_model + kv_gib, 2)
                    if vram_model and kv_gib else None),
        "note": "模型权重按 GGUF 文件大小计；KV 按 --cache-type 精确换算",
    }
    return out


async def _ollama_efficiency(adapter: "OllamaAdapter") -> Dict[str, Any]:
    out: Dict[str, Any] = {"kind": "ollama", "notes": [], "model": None,
                           "kv_cache": None, "flops": None}
    try:
        async with adapter._client(10) as c:  # noqa: SLF001
            r = await c.get(f"{adapter.base_url}/api/ps")
            r.raise_for_status()
            models = r.json().get("models", [])
        out["loaded_models"] = [
            {"name": m.get("name"),
             "size_gib": round((m.get("size") or 0) / 2 ** 30, 2),
             "vram_gib": round((m.get("size_vram") or 0) / 2 ** 30, 2),
             "ctx": (m.get("details") or {}).get("context_length"),
             "quant": (m.get("details") or {}).get("quantization_level")}
            for m in models
        ]
        if models:
            m0 = models[0]
            out["model"] = {
                "file": m0.get("name"),
                "size_gib": round((m0.get("size") or 0) / 2 ** 30, 2),
                "vram_gib": round((m0.get("size_vram") or 0) / 2 ** 30, 2),
                "parameters": (m0.get("details") or {}).get("parameter_size"),
                "quant": (m0.get("details") or {}).get("quantization_level"),
            }
            out["notes"].append(
                "Ollama 以 blob 形式存储权重，KV Cache 由后端按 num_ctx 自行分配，"
                "此处只给出实测显存占用；如需精确 KV 折算，请使用 llama.cpp 后端。")
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"/api/ps 不可用: {type(exc).__name__}: {exc}")
    return out


async def _demo_efficiency(model_hint: str = "") -> Dict[str, Any]:
    """内置演示后端的效率指标。

    这里**不伪造**：模型超参是按常见 7B 架构（Qwen2/Llama 系）写死的档位，
    但 KV Cache / 权重显存 / FLOPs 全部走 ``metrics_ext`` 里的**真实公式**
    计算，并把 ``simulated`` 置为 True，让「算得对不对」这件事本身可被验证。
    """
    from .metrics_ext import flops_theory, kv_cache_bytes

    spec = next((m for m in _DEMO_MODELS if m["name"] == model_hint), _DEMO_MODELS[0])
    # 每个档位的超参按官方 config.json 抄录（层数 / KV头数 / head_dim / 训练上下文 /
    # 参数量 / 隐藏维度 / 量化 / 文件体积）。参数量取官方公布值，用于 FLOPs 口径。
    # 用**具名字典**而不是元组，避免再出现「隐藏维度填进上下文槽位」这类位置错填。
    arch: Dict[str, Dict[str, Any]] = {
        "demo-qwen-7b": {"architecture": "qwen2", "n_layer": 28, "n_head_kv": 4,
                         "head_dim": 128, "hidden_size": 3584, "vocab_size": 152064,
                         "ctx_train": 32768, "params": 7615616512,
                         "quant": "Q4_K_M", "size_gib": 4.2},
        "demo-llama3-8b": {"architecture": "llama", "n_layer": 32, "n_head_kv": 8,
                           "head_dim": 128, "hidden_size": 4096, "vocab_size": 128256,
                           "ctx_train": 8192, "params": 8030261248,
                           "quant": "Q4_K_M", "size_gib": 4.9},
        "demo-deepseek-r1-7b": {"architecture": "qwen2", "n_layer": 28, "n_head_kv": 4,
                                "head_dim": 128, "hidden_size": 3584, "vocab_size": 152064,
                                "ctx_train": 32768, "params": 7615616512,
                                "quant": "Q5_K_M", "size_gib": 5.3},
        "demo-gemma2-2b": {"architecture": "gemma2", "n_layer": 26, "n_head_kv": 4,
                           "head_dim": 256, "hidden_size": 2304, "vocab_size": 256000,
                           "ctx_train": 8192, "params": 2614342240,
                           "quant": "Q8_0", "size_gib": 2.6},
    }
    a = arch.get(spec["name"]) or arch["demo-qwen-7b"]
    _a = a["architecture"]
    n_layer = a["n_layer"]
    n_kv = a["n_head_kv"]
    head_dim = a["head_dim"]
    ctx_train = a["ctx_train"]
    params = a["params"]
    quant = a["quant"]
    size_gib = a["size_gib"]
    n_ctx = 8192
    slots = int(spec.get("batch") or 1)
    kv = kv_cache_bytes(n_layer, n_ctx, n_kv, head_dim, head_dim, "f16", slots)
    weights_mib = round(size_gib * 1024, 1)
    total_mib = round(weights_mib + (kv.get("total_mib") or 0), 1)
    table = []
    for c in (4096, 8192, 16384, 32768, 65536):
        k = kv_cache_bytes(n_layer, c, n_kv, head_dim, head_dim, "f16", 1)
        if k.get("ok"):
            table.append({"ctx": c, "per_slot_gib": k["total_gib"],
                          "slots_total_gib": round(k["total_gib"] * slots, 2)})
    return {
        "kind": "demo", "simulated": True,
        "server": {
            "ctx_size": n_ctx, "total_slots": slots,
            "n_gpu_layers": 999, "parallel": slots,
            "cache_type_k": "f16", "cache_type_v": "f16",
        },
        "model": {
            "name": spec["name"], "architecture": _a, "file": f"{spec['name']}.gguf",
            "n_layer": n_layer, "n_head_kv": n_kv, "head_dim": head_dim,
            "hidden_size": a["hidden_size"], "vocab_size": a["vocab_size"],
            "context_length": ctx_train, "n_ctx": n_ctx,
            "params": params, "params_b": round(params / 1e9, 2),
            "quantization": quant, "file_size_mib": weights_mib,
            "weights_mib": weights_mib, "total_vram_mib": total_mib,
            "parameter_count_source": "演示档位内置参数（模拟）",
            "context_source": "演示档位内置（模拟）",
        },
        "kv_cache": {
            "current": kv, "current_ctx": n_ctx, "slots": slots,
            "cache_type_k": "f16", "cache_type_v": "f16", "table": table,
            "total_mib": kv.get("total_mib"), "kv_type": "f16",
            "n_ctx": n_ctx, "n_layer": n_layer, "per_slot_mib": kv.get("per_slot_mib"),
            "formula_note": "bytes = slots × 层数 × ctx × KV头数 × (K维+V维) × 每元素字节数",
        },
        "flops": {
            "params": params, "params_b": round(params / 1e9, 2),
            "flops_per_token": flops_theory(params, 1),
            "formula": "理论前向 FLOPs ≈ 2 × 参数量 × token 数（不含注意力平方项）",
        },
        "notes": [
            "内置演示后端：模型超参取自常见 7B 档位，KV Cache / 权重显存 / FLOPs "
            "均由真实公式计算，仅输入参数是模拟的，结果已标注 simulated。",
        ],
    }


def _normalize_efficiency(backend: str, eff: Dict[str, Any]) -> None:
    """把各后端的效率输出归一成前端统一读取的一组字段（就地把结果写回 eff）。

    没有做归一化时，llama.cpp / Ollama / demo 三套字段名各不相同，
    前端就得写三套分支；这里统一成 model / kv_cache / flops 三个固定形状。
    """
    from .metrics_ext import flops_theory

    m_in = eff.get("model") or {}
    srv = eff.get("server") or {}
    kv_in = eff.get("kv_cache") or {}
    fl_in = eff.get("flops") or {}
    vram = eff.get("vram") or {}
    gpu = eff.get("gpu") or {}
    kv_cur = kv_in.get("current") or kv_in

    model: Dict[str, Any] = {
        "name": m_in.get("name") or m_in.get("file") or "",
        "architecture": m_in.get("architecture"),
        "params": m_in.get("params") or m_in.get("parameters"),
        "params_b": m_in.get("params_b"),
        "quantization": m_in.get("quantization") or m_in.get("file_type")
                        or m_in.get("quant") or m_in.get("model_ftype"),
        "parameter_count_source": m_in.get("parameter_source")
                                  or m_in.get("parameter_count_source"),
        "note": m_in.get("note"),
    }
    # 上下文长度
    ctx = (m_in.get("context_length") or m_in.get("ctx_train")
           or srv.get("ctx_size") or kv_in.get("n_ctx"))
    model["context_length"] = ctx
    model["context_source"] = m_in.get("context_source") or (
        "llama-server -c / GGUF 元数据" if backend == "llama_cpp" else
        "GGUF 元数据" if backend == "ollama" else
        "演示档位内置（模拟）" if backend == "demo" else None)
    # 层数 / KV 头数
    model["n_layer"] = m_in.get("n_layer") or m_in.get("layers") or kv_cur.get("n_layer")
    model["n_head_kv"] = m_in.get("n_head_kv") or m_in.get("kv_heads") or kv_cur.get("n_head_kv")
    # 维度（KV Cache 折算的三个输入：层数 / KV头数 / head_dim）
    model["head_dim"] = (m_in.get("head_dim") or kv_cur.get("head_dim")
                         or (m_in.get("key_length") if isinstance(kv_cur, dict) else None))
    model["hidden_size"] = m_in.get("hidden_size") or m_in.get("n_embd")
    model["vocab_size"] = m_in.get("vocab_size") or m_in.get("n_vocab")
    # 文件/权重/合计显存（MiB）
    size_gib = m_in.get("size_gib") or vram.get("model_gib")
    model["file_size_mib"] = (m_in.get("file_size_mib")
                              or (round(size_gib * 1024, 1) if size_gib else None)
                              or m_in.get("weights_mib"))
    model["weights_mib"] = (m_in.get("weights_mib") or m_in.get("file_size_mib")
                            or (round(size_gib * 1024, 1) if size_gib else None)
                            or gpu.get("weights_mib"))
    model["total_vram_mib"] = (m_in.get("total_vram_mib")
                               or (round(((vram.get("sum_gib") or 0) * 1024), 1)
                                   if vram.get("sum_gib") else None))
    if model["total_vram_mib"] is None and model["weights_mib"]:
        kvm = (kv_cur.get("total_mib") if isinstance(kv_cur, dict) else None) \
            or kv_in.get("total_mib")
        if kvm:
            model["total_vram_mib"] = round(model["weights_mib"] + kvm, 1)

    params = model.get("params")
    if not params and model.get("params_b"):
        params = int(model["params_b"] * 1e9)
    flops: Dict[str, Any] = {"params": params, "params_b": model.get("params_b")}
    flops["flops_per_token"] = (fl_in.get("flops_per_token")
                                or (flops_theory(params, 1) if params else None))
    flops["formula"] = fl_in.get("formula") or (
        "理论前向 FLOPs ≈ 2 × 参数量 × token 数（不含注意力平方项）")
    # 若已有实测吞吐，可由用户自行计算 MFU；这里只给理论上限口径
    flops["theoretical_tflops_1s"] = (round(params * 2 / 1e12, 3) if params else None)

    kv: Dict[str, Any] = {
        "total_mib": kv_in.get("total_mib") or (kv_cur.get("total_mib")
                                                if isinstance(kv_cur, dict) else None),
        "kv_type": kv_in.get("kv_type") or kv_in.get("cache_type_k")
                   or (kv_cur.get("kv_type") if isinstance(kv_cur, dict) else None),
        "n_ctx": kv_in.get("n_ctx") or kv_in.get("current_ctx") or ctx,
        "n_layer": kv_in.get("n_layer") or model.get("n_layer"),
        "slots": kv_in.get("slots"),
        "per_slot_mib": kv_in.get("per_slot_mib")
                        or (kv_cur.get("per_slot_mib") if isinstance(kv_cur, dict) else None),
        "table": kv_in.get("table") or [],
        "formula_note": kv_in.get("formula_note"),
    }

    eff["model"] = model
    eff["kv_cache"] = kv
    eff["flops"] = flops


async def efficiency_info(conn: Dict[str, Any], model: str = "") -> Dict[str, Any]:
    """聚合「效率与资源」指标：原生指标 + KV Cache 精确折算 + FLOPs 理论值。

    关键点：**不猜测**。拿不到就返回原因（如远端服务读不到 GGUF），
    前端会显示「不可用」而不是一个看起来像真数据的假数字。
    """
    adapter = build_adapter(conn)
    out: Dict[str, Any] = {
        "ok": True, "conn_id": conn.get("id"), "conn_name": conn.get("name"),
        "backend": conn.get("backend"), "base_url": conn.get("base_url"),
        "ppl_supported": bool(getattr(adapter, "supports_ppl", False)),
        "native": {}, "notes": [],
    }
    try:
        out["native"] = await adapter.native_metrics()
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"原生指标获取失败: {type(exc).__name__}")

    backend = (conn.get("backend") or "").lower()
    try:
        if backend == "llama_cpp":
            eff = await _llamacpp_efficiency(adapter, model)
        elif backend == "ollama":
            eff = await _ollama_efficiency(adapter)
        elif backend == "demo":
            eff = await _demo_efficiency(model or conn.get("default_model") or "")
        else:
            eff = {"kind": backend or "unknown",
                   "notes": ["该后端未提供 KV Cache / 算力元数据接口"], "model": None,
                   "kv_cache": None, "flops": None}
    except Exception as exc:  # noqa: BLE001
        eff = {"kind": backend, "notes": [f"效率信息采集异常: {type(exc).__name__}: {exc}"],
               "model": None, "kv_cache": None, "flops": None}
    try:
        _normalize_efficiency(backend, eff)
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"效率字段归一化失败: {type(exc).__name__}: {exc}")
    base_notes = list(out.get("notes") or [])
    out.update(eff)
    out["notes"] = base_notes + list(eff.get("notes") or [])
    return out


async def cold_start_probe(conn: Dict[str, Any], model: str = "") -> Dict[str, Any]:
    """冷启动 / 模型加载耗时探测。

    * **Ollama**：先用 ``keep_alive: 0`` 卸载模型，再发一次请求读 ``load_duration``
      —— 这是真实的权重加载耗时。
    * **llama.cpp**：没有远程卸载接口（模型在进程启动时就加载好了），
      因此只测「首请求延迟」作为代理指标，并明确说明无法测得真实冷启动。
    * **demo**：返回合成值并标注 simulated。
    """
    adapter = build_adapter(conn)
    backend = (conn.get("backend") or "").lower()
    model = model or conn.get("default_model") or ""

    if backend == "ollama":
        try:
            async with adapter._client(180) as c:  # noqa: SLF001
                await c.post(f"{adapter.base_url}/api/generate",
                             json={"model": model, "prompt": "", "keep_alive": 0,
                                   "stream": False})
            async with adapter._client(180) as c:  # noqa: SLF001
                t0 = time.perf_counter()
                r = await c.post(f"{adapter.base_url}/api/generate", json={
                    "model": model, "prompt": "hi", "stream": False,
                    "options": {"num_predict": 1}})
                r.raise_for_status()
                obj = r.json()
            total_ms = (time.perf_counter() - t0) * 1000
            load_ms = (obj.get("load_duration") or 0) / 1e6
            return {
                "ok": True, "backend": "ollama", "model": model,
                "load_ms": round(load_ms, 1), "total_ms": round(total_ms, 1),
                "prompt_eval_ms": round((obj.get("prompt_eval_duration") or 0) / 1e6, 1),
                "eval_ms": round((obj.get("eval_duration") or 0) / 1e6, 1),
                "measured_by": "Ollama load_duration（真实权重加载耗时）",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": "ollama", "model": model,
                    "reason": f"冷启动探测失败: {type(exc).__name__}: {exc}"}

    if backend == "llama_cpp":
        # 若服务端启用了 sleep 模式，尝试唤醒并计时
        try:
            async with adapter._client(180) as c:  # noqa: SLF001
                pr = await c.get(f"{adapter.base_url}/props")
                props = pr.json() if pr.status_code == 200 else {}
                if props.get("is_sleeping"):
                    t0 = time.perf_counter()
                    wr = await c.post(f"{adapter.base_url}/wake")
                    if wr.status_code < 400:
                        return {"ok": True, "backend": "llama_cpp", "model": model,
                                "load_ms": round((time.perf_counter() - t0) * 1000, 1),
                                "measured_by": "llama.cpp /wake（从 sleep 状态唤醒耗时）"}
        except Exception:  # noqa: BLE001
            pass
        # 代理指标：一次最小请求的首 Token 延迟
        try:
            t0 = time.perf_counter()
            r = await adapter.chat_collect(model or "local", [{"role": "user", "content": "hi"}],
                                           temperature=0.0, max_tokens=4, stream=False)
            ttft = r.get("ttft_ms") or (time.perf_counter() - t0) * 1000
            return {
                "ok": True, "backend": "llama_cpp", "model": model,
                "first_request_ttft_ms": round(ttft, 1),
                "load_ms": None,
                "measured_by": "首请求首 Token 延迟（代理指标）",
                "reason": "llama.cpp 在进程启动时即加载权重，HTTP 接口无法卸载模型，"
                          "因此真实冷启动时间需重启服务测量；这里给出首请求延迟作为代理。",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": "llama_cpp", "model": model,
                    "reason": f"代理探测失败: {type(exc).__name__}: {exc}"}

    if backend == "demo":
        r = random.Random(hash(model) & 0xFFFF)
        return {"ok": True, "backend": "demo", "model": model,
                "load_ms": round(r.uniform(1200, 4200), 1), "simulated": True,
                "measured_by": "内置演示后端的合成值（不代表真实模型）"}

    return {"ok": False, "backend": backend, "model": model,
            "reason": f"{adapter.label} 未提供冷启动测量接口"}
