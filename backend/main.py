"""FastAPI 主入口：REST 路由 + WebSocket + 静态前端托管。

启动方式::

    uvicorn backend.main:app --host 0.0.0.0 --port 8080

打开 http://localhost:8080 即可使用完整控制台。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (alerts, catalog as metric_catalog, chat, collector, config, db,
               evaluator, probe, tester)
from .ws import broadcaster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("llm-monitor.main")

_alert_task: Optional[asyncio.Task] = None


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    log.info("数据库就绪: %s", config.DB_PATH)

    collector_task = asyncio.create_task(collector.collector_loop())

    async def health_loop() -> None:
        """每 10 秒对所有连接档案做一次心跳探测，更新在线状态灯。"""
        while True:
            for conn in db.list_connections():
                if conn.get("backend") == "demo":
                    db.mark_connection_status(conn["id"], "online", "demo-simulator/1.0")
                    continue
                try:
                    ad = probe.build_adapter(conn)
                    result = await asyncio.wait_for(ad.probe(), timeout=config.CONNECT_TIMEOUT + 4)
                    db.mark_connection_status(
                        conn["id"], "online" if result["ok"] else "offline",
                        result.get("version") or "")
                except asyncio.TimeoutError:
                    db.mark_connection_status(conn["id"], "timeout")
                except Exception:  # noqa: BLE001
                    db.mark_connection_status(conn["id"], "offline")
            await broadcaster.broadcast({"type": "connections_changed"})
            try:
                await asyncio.wait_for(asyncio.sleep(config.HEALTH_INTERVAL), timeout=config.HEALTH_INTERVAL + 1)
            except asyncio.TimeoutError:
                pass

    async def alert_loop() -> None:
        while True:
            try:
                fired = alerts.evaluate()
                if fired:
                    await broadcaster.broadcast({"type": "alerts", "new": fired})
            except Exception as exc:  # noqa: BLE001
                log.warning("告警评估异常: %s", exc)
            await asyncio.sleep(5)

    global _alert_task
    health_task = asyncio.create_task(health_loop())
    _alert_task = asyncio.create_task(alert_loop())
    log.info("服务已启动 → http://localhost:%d", config.PORT)
    try:
        yield
    finally:
        for t in (collector_task, health_task, _alert_task):
            t.cancel()
        await asyncio.gather(collector_task, health_task, _alert_task,
                             return_exceptions=True)
        log.info("服务已停止")


app = FastAPI(title="本地大模型实时性能监控测试平台", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------
class ConnectionIn(BaseModel):
    name: str = Field(..., min_length=1)
    backend: str = "ollama"
    base_url: str = Field(..., min_length=1)
    api_key: str = ""
    default_model: str = ""


class ProbeIn(BaseModel):
    backend: str = "ollama"
    base_url: str = Field(..., min_length=1)
    api_key: str = ""


class ChatMessage(BaseModel):
    role: str = "user"
    content: str = ""


class SingleIn(BaseModel):
    conn_id: int
    model: str = ""
    prompt: str = ""
    system: str = ""
    messages: Optional[List[ChatMessage]] = None
    temperature: float = 0.7
    top_p: float = 0.9
    # max_tokens<=0 约定为「不限制输出长度」（各后端分别映射为 -1 / 省略字段）
    max_tokens: int = config.CHAT_DEFAULT_MAX_TOKENS
    num_ctx: Optional[int] = None
    stream: bool = True
    save: bool = True
    # ---- 对话测试（多轮 + 记忆）----
    session_id: Optional[int] = None       # 归属会话；为空则退化成一次性单条测试
    mode: str = "chat"                     # chat | continue | regenerate
    echo_user: bool = True                 # 是否把提问写入会话（重新生成时为 False）
    use_memory: bool = True                # 是否注入会话的长期记忆
    ctx_budget: Optional[int] = None       # 覆盖会话的上下文预算（token）


class ChatSessionIn(BaseModel):
    title: str = ""
    conn_id: Optional[int] = None
    model: str = ""
    system: str = ""
    memory: str = ""
    ctx_budget: Optional[int] = None
    keep_turns: Optional[int] = None


class ChatSessionPatch(BaseModel):
    title: Optional[str] = None
    conn_id: Optional[int] = None
    model: Optional[str] = None
    system: Optional[str] = None
    memory: Optional[str] = None
    ctx_budget: Optional[int] = None
    keep_turns: Optional[int] = None


class BenchmarkIn(BaseModel):
    conn_id: int
    model: str = ""
    suite_id: str = "smoke"
    case_ids: Optional[List[str]] = None
    overrides: Dict[str, Any] = Field(default_factory=dict)


class LoadTestIn(BaseModel):
    conn_id: int
    model: str = ""
    prompts: List[str] = Field(default_factory=list)
    concurrency: int = 8
    duration: int = 60
    mode: str = "fixed"  # fixed | stage
    start_concurrency: int = 1
    max_concurrency: int = 16
    stage_seconds: int = 30
    max_tokens: int = 256
    temperature: float = 0.7


class StopIn(BaseModel):
    run_id: Optional[int] = None


class EvalIn(BaseModel):
    conn_id: int
    model: str = ""
    suite_id: str
    mode: str = "quick"                  # quick | standard | full
    allow_code_exec: bool = False        # 代码题沙箱执行（HumanEval/MBPP 必需）
    judge_conn_id: Optional[int] = None  # LLM-as-judge 用另一个连接（可为空）
    judge_model: str = ""
    overrides: Dict[str, Any] = Field(default_factory=dict)


class ColdStartIn(BaseModel):
    conn_id: int
    model: str = ""


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------
def _require_conn(conn_id: int) -> Dict[str, Any]:
    conn = db.get_connection(conn_id)
    if not conn:
        raise HTTPException(404, f"连接档案 #{conn_id} 不存在")
    return conn


def _adapter_or_400(conn: Dict[str, Any]) -> probe.BaseAdapter:
    try:
        return probe.build_adapter(conn)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"无法构建适配器: {exc}") from exc


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True, "time": time.time(), "version": app.version,
        "ws_clients": broadcaster.count,
        "active_runs": len(tester.list_active()),
        "collector_alive": bool(collector.last_sample),
        "db": str(config.DB_PATH),
    }


@app.get("/api/system/info")
async def system_info() -> Dict[str, Any]:
    return collector.system_info()


@app.get("/api/backends/presets")
async def backend_presets() -> List[Dict[str, Any]]:
    return probe.BACKEND_PRESETS


@app.get("/api/overview")
async def overview() -> Dict[str, Any]:
    info = collector.system_info()
    return {
        "system": info,
        "stats": collector.stats.snapshot(),
        "db": db.get_stats_overview(),
        "active_runs": tester.list_active(),
        "unacked_alerts": len(db.list_alerts(200, acked=0)),
    }


# --------------------------------------------------------------------------
# 连接档案
# --------------------------------------------------------------------------
@app.get("/api/connections")
async def list_connections() -> List[Dict[str, Any]]:
    return db.list_connections()


@app.post("/api/connections")
async def create_connection(payload: ConnectionIn) -> Dict[str, Any]:
    cid = db.add_connection(payload.name.strip(), payload.backend.strip(),
                            payload.base_url.strip(), payload.api_key,
                            payload.default_model.strip())
    return {"id": cid, "ok": True}


@app.put("/api/connections/{conn_id}")
async def update_connection(conn_id: int, payload: ConnectionIn) -> Dict[str, Any]:
    _require_conn(conn_id)
    db.update_connection(conn_id, name=payload.name.strip(), backend=payload.backend.strip(),
                         base_url=payload.base_url.strip(), api_key=payload.api_key,
                         default_model=payload.default_model.strip())
    return {"ok": True}


@app.delete("/api/connections/{conn_id}")
async def delete_connection(conn_id: int) -> Dict[str, Any]:
    _require_conn(conn_id)
    db.delete_connection(conn_id)
    return {"ok": True}


@app.post("/api/connections/{conn_id}/test")
async def test_connection(conn_id: int) -> Dict[str, Any]:
    conn = _require_conn(conn_id)
    ad = _adapter_or_400(conn)
    result = await ad.probe()
    db.mark_connection_status(conn_id, "online" if result["ok"] else "offline",
                              result.get("version") or "")
    result["native"] = await ad.native_metrics() if result["ok"] else {}
    return result


@app.post("/api/connections/probe")
async def probe_connection(payload: ProbeIn) -> Dict[str, Any]:
    """在保存档案之前先探测一次（连接表单的"测试连接"按钮）。"""
    ad = probe.build_adapter(payload.model_dump())
    result = await ad.probe()
    result["native"] = await ad.native_metrics() if result["ok"] else {}
    return result


@app.get("/api/models")
async def list_models(conn_id: int = Query(...)) -> Dict[str, Any]:
    conn = _require_conn(conn_id)
    ad = _adapter_or_400(conn)
    try:
        models = await ad.list_models()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"拉取模型列表失败: {exc}") from exc
    native = await ad.native_metrics()
    return {"models": models, "native": native}


# --------------------------------------------------------------------------
# 单条对话测试（SSE 流式）
# --------------------------------------------------------------------------
def _require_session(session_id: int) -> Dict[str, Any]:
    row = db.get_chat_session(session_id)
    if not row:
        raise HTTPException(404, f"会话 #{session_id} 不存在")
    return row


def _prepare_chat_context(payload: SingleIn, session: Dict[str, Any]
                          ) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
    """把会话历史 + 长期记忆装配成这次要发给模型的 messages。

    返回 ``(messages, chat_ctx)``；``chat_ctx`` 会交给 tester 在收尾时落库。
    """
    mode = (payload.mode or "chat").lower()
    if mode not in ("chat", "continue", "regenerate"):
        raise HTTPException(400, f"未知的对话模式：{payload.mode}")

    history = chat.load_history(session["id"])
    if mode == "continue":
        instruction = chat.CONTINUE_INSTRUCTION
        persist_user_text = ""
    else:
        if not (payload.prompt or "").strip():
            raise HTTPException(400, "提问内容不能为空")
        instruction = payload.prompt
        # regenerate：上一轮的提问仍在会话里，不重复写入
        persist_user_text = payload.prompt if mode == "chat" and payload.echo_user else ""

    messages, info = chat.build_context(
        session, history, instruction,
        system_override=payload.system,
        budget=payload.ctx_budget,
        reserve=max(0, int(payload.max_tokens or 0)),
        use_memory=payload.use_memory,
        # 重新生成要以「提问」结尾：末尾那条旧回答要被丢掉，否则模型会看到
        # 一个「已答完又问一遍」的上下文（实测演示后端会错把本轮提问当成上一轮）
        regenerate=(mode == "regenerate"),
    )
    ctx = {
        "session_id": session["id"],
        "user_text": persist_user_text,
        "echo_user": bool(persist_user_text),
        "mode": mode,
        "continued": mode == "continue",
        "context": info,
    }
    return messages, ctx


@app.post("/api/test/single")
async def test_single(payload: SingleIn) -> StreamingResponse:
    conn = _require_conn(payload.conn_id)
    ad = _adapter_or_400(conn)
    model = payload.model or conn.get("default_model") or ""

    chat_ctx: Optional[Dict[str, Any]] = None
    if payload.session_id:
        session = _require_session(payload.session_id)
        # 会话里记着的模型优先，便于「切回旧会话继续聊」
        if not payload.model and session.get("model"):
            model = session["model"]
        messages, chat_ctx = _prepare_chat_context(payload, session)
        if chat_ctx["mode"] == "chat" and chat_ctx["user_text"]:
            chat.ensure_title(session, chat_ctx["user_text"])
    elif payload.messages:
        messages = [m.model_dump() for m in payload.messages]
    else:
        messages = []
        if payload.system:
            messages.append({"role": "system", "content": payload.system})
        messages.append({"role": "user", "content": payload.prompt})

    params = {
        "temperature": payload.temperature, "top_p": payload.top_p,
        "max_tokens": payload.max_tokens, "num_ctx": payload.num_ctx,
        "stream": payload.stream,
    }

    async def event_stream():
        try:
            async for evt in tester.stream_single(conn, ad, model, messages, params,
                                                  save=payload.save, chat_ctx=chat_ctx):
                yield f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"
        except Exception as exc:  # noqa: BLE001
            log.exception("单条测试流异常")
            yield f"data: {json.dumps({'event': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# --------------------------------------------------------------------------
# 对话会话（记忆的载体）
# --------------------------------------------------------------------------
@app.get("/api/chat/sessions")
async def chat_sessions() -> Dict[str, Any]:
    return {"sessions": [chat.session_public(r) for r in db.list_chat_sessions()],
            "defaults": {"ctx_budget": config.CHAT_CTX_BUDGET,
                         "keep_turns": config.CHAT_KEEP_TURNS,
                         "max_tokens": config.CHAT_DEFAULT_MAX_TOKENS,
                         "max_msg_chars": config.CHAT_MAX_MSG_CHARS}}


@app.post("/api/chat/sessions")
async def chat_session_create(payload: ChatSessionIn) -> Dict[str, Any]:
    conn_id = payload.conn_id
    model = payload.model
    if conn_id is None:
        conns = db.list_connections()
        if not conns:
            raise HTTPException(400, "还没有任何连接档案，请先在「连接管理」里新增")
        conn_id = conns[0]["id"]
    if not model:
        cn = db.get_connection(int(conn_id))
        model = (cn or {}).get("default_model") or ""
    sid = db.create_chat_session(payload.title, int(conn_id), model,
                                 system=payload.system, memory=payload.memory,
                                 ctx_budget=payload.ctx_budget,
                                 keep_turns=payload.keep_turns)
    return {"session": chat.session_public(_require_session(sid))}


@app.get("/api/chat/sessions/{sid}")
async def chat_session_detail(sid: int) -> Dict[str, Any]:
    row = _require_session(sid)
    msgs = [chat.message_public(m) for m in db.list_chat_messages(sid)]
    turns = chat.load_history(sid)
    _, info = chat.build_context(row, turns, "")
    return {"session": chat.session_public(row), "messages": msgs,
            "context": info,
            "defaults": {"ctx_budget": config.CHAT_CTX_BUDGET,
                         "keep_turns": config.CHAT_KEEP_TURNS}}


@app.patch("/api/chat/sessions/{sid}")
async def chat_session_update(sid: int, payload: ChatSessionPatch) -> Dict[str, Any]:
    _require_session(sid)
    fields = payload.model_dump(exclude_unset=True)
    db.update_chat_session(sid, **fields)
    return {"session": chat.session_public(_require_session(sid))}


@app.delete("/api/chat/sessions/{sid}")
async def chat_session_delete(sid: int) -> Dict[str, Any]:
    _require_session(sid)
    db.delete_chat_session(sid)
    return {"ok": True, "deleted": sid}


@app.delete("/api/chat/sessions/{sid}/messages")
async def chat_messages_clear(sid: int, from_id: Optional[int] = Query(None)) -> Dict[str, Any]:
    """清空会话消息；带 ``from_id`` 时只删除该 id 及之后的消息（编辑重发/重新生成）。"""
    _require_session(sid)
    if from_id:
        n = db.delete_chat_messages_from(sid, int(from_id))
    else:
        n = db.clear_chat_messages(sid)
    db.refresh_chat_session_stats(sid)
    return {"ok": True, "deleted": n,
            "session": chat.session_public(_require_session(sid))}


@app.delete("/api/chat/sessions/{sid}/messages/{mid}")
async def chat_message_delete(sid: int, mid: int) -> Dict[str, Any]:
    _require_session(sid)
    db.delete_chat_message(sid, mid)
    db.refresh_chat_session_stats(sid)
    return {"ok": True, "session": chat.session_public(_require_session(sid))}


@app.post("/api/chat/sessions/{sid}/memory/distill")
async def chat_memory_distill(sid: int, payload: ChatSessionPatch) -> Dict[str, Any]:
    """让模型把当前对话提炼成长期记忆条目（写入会话的 memory 字段）。"""
    row = _require_session(sid)
    conn_id = payload.conn_id or row.get("conn_id")
    if not conn_id:
        raise HTTPException(400, "该会话没有绑定连接档案，无法调用模型")
    conn = _require_conn(int(conn_id))
    ad = _adapter_or_400(conn)
    model = payload.model or row.get("model") or conn.get("default_model") or ""
    history = chat.load_history(sid)
    result = await chat.distill_memory(ad, model, row, history)
    result["session"] = chat.session_public(_require_session(sid))
    result["messages_used"] = len(history)
    return result


# --------------------------------------------------------------------------
# 基准测试套件
# --------------------------------------------------------------------------
@app.get("/api/test/suites")
async def get_suites() -> Dict[str, Any]:
    return {"suites": tester.list_suites(), "cases": tester.list_cases()}


def _guard(run_id: int, factory):
    """把长任务包装成后台协程，并在异常/结束时清理运行态。"""
    async def runner() -> None:
        try:
            await factory()
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 #%s 执行失败", run_id)
            db.finish_run(run_id, {"duration_ms": 0, "p50_e2e_ms": None},
                          status="error")
            await broadcaster.broadcast({
                "type": "run_done", "run_id": run_id, "status": "error",
                "error": str(exc),
            })
        finally:
            tester._active_runs.pop(run_id, None)  # noqa: SLF001

    return asyncio.create_task(runner())


@app.post("/api/test/benchmark")
async def test_benchmark(payload: BenchmarkIn) -> Dict[str, Any]:
    conn = _require_conn(payload.conn_id)
    ad = _adapter_or_400(conn)
    model = payload.model or conn.get("default_model") or ""
    run_id = db.create_run(conn["id"], conn.get("name", ""), model, "benchmark",
                           payload.suite_id, 1, collector.env_snapshot(),
                           {"suite_id": payload.suite_id,
                            "case_ids": payload.case_ids,
                            "overrides": payload.overrides})
    _guard(run_id, lambda: tester.run_benchmark(
        run_id, conn, ad, model, payload.suite_id, payload.overrides, payload.case_ids))
    return {"run_id": run_id, "ok": True}


@app.post("/api/test/loadtest/start")
async def test_loadtest_start(payload: LoadTestIn) -> Dict[str, Any]:
    if tester.list_active():
        raise HTTPException(409, "已有测试任务在运行，请先停止后再启动新的压测")
    conn = _require_conn(payload.conn_id)
    ad = _adapter_or_400(conn)
    model = payload.model or conn.get("default_model") or ""
    cfg = payload.model_dump()
    run_id = db.create_run(conn["id"], conn.get("name", ""), model, "loadtest",
                           "loadtest", payload.concurrency, collector.env_snapshot(), cfg)
    _guard(run_id, lambda: tester.run_loadtest(run_id, conn, ad, model, cfg))
    return {"run_id": run_id, "ok": True,
            "plan": tester.build_stage_plan(cfg)}


@app.post("/api/test/stop")
async def test_stop(payload: StopIn = Body(default=StopIn())) -> Dict[str, Any]:
    if payload.run_id:
        ok = tester.request_stop(payload.run_id)
        return {"ok": ok, "stopped": [payload.run_id] if ok else []}
    stopped = list(tester._active_runs.keys())  # noqa: SLF001
    tester.stop_all()
    return {"ok": True, "stopped": stopped}


@app.get("/api/test/active")
async def test_active() -> List[Dict[str, Any]]:
    return tester.list_active()


# --------------------------------------------------------------------------
# 评测中心（能力/质量/安全三大类）
# --------------------------------------------------------------------------
@app.get("/api/eval/suites")
async def eval_suites() -> Dict[str, Any]:
    """列出全部评测套件（按四大类分组），以及可选的运行模式。"""
    return evaluator.list_suites()


@app.get("/api/eval/estimate")
async def eval_estimate(suite_id: str = Query(...), mode: str = "quick",
                        prefill_tps: float = 8.0, decode_tps: float = 24.0) -> Dict[str, Any]:
    return evaluator.estimate_cost(suite_id, mode, prefill_tps, decode_tps)


@app.post("/api/eval/start")
async def eval_start(payload: EvalIn) -> Dict[str, Any]:
    if tester.list_active():
        raise HTTPException(409, "已有测试任务在运行，请先停止后再启动新的评测")
    conn = _require_conn(payload.conn_id)
    ad = _adapter_or_400(conn)
    model = payload.model or conn.get("default_model") or ""

    if payload.suite_id not in {s["id"] for s in evaluator.load_suites()}:
        raise HTTPException(404, f"评测套件 {payload.suite_id} 不存在")
    suite = evaluator.get_suite(payload.suite_id)
    if suite and suite.get("metric") == "pass_at_k" and not payload.allow_code_exec:
        raise HTTPException(400, "该套件需要执行模型生成的代码，请先勾选「允许执行代码」")

    judge_adapter = None
    if payload.judge_conn_id:
        jconn = _require_conn(payload.judge_conn_id)
        judge_adapter = _adapter_or_400(jconn)

    cfg = {
        "suite_id": payload.suite_id, "mode": payload.mode,
        "allow_code_exec": payload.allow_code_exec,
        "overrides": payload.overrides,
    }
    run_id = db.create_run(conn["id"], conn.get("name", ""), model, "eval",
                           payload.suite_id, 1, collector.env_snapshot(), cfg)
    _guard(run_id, lambda: evaluator.run_eval(
        run_id, conn, ad, model, cfg,
        judge_adapter=judge_adapter, judge_model=payload.judge_model))
    return {"run_id": run_id, "ok": True,
            "estimate": evaluator.estimate_cost(payload.suite_id, payload.mode)}


@app.get("/api/eval/{run_id}")
async def eval_result(run_id: int) -> Dict[str, Any]:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "评测记录不存在")
    items = db.get_eval_items(run_id)
    for key in ("env_json", "config_json", "summary_json"):
        if run.get(key):
            try:
                run[key.replace("_json", "")] = json.loads(run[key])
            except (ValueError, TypeError):
                pass
    summary = (run.get("summary") or {}).get("eval") if isinstance(run.get("summary"), dict) \
        else None
    return {"run": run, "items": items, "summary": summary}


@app.get("/api/eval/{run_id}/export")
async def eval_export(run_id: int, fmt: str = "json") -> Any:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "评测记录不存在")
    items = db.get_eval_items(run_id)
    if fmt.lower() == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["run_id", "item_id", "case_label", "ok", "metric", "detail",
                         "ttft_ms", "e2e_ms", "prefill_tps", "decode_tps",
                         "status", "error", "answer"])
        for it in items:
            writer.writerow([run_id, it.get("item_id"), it.get("case_label"),
                             it.get("ok"), it.get("metric"), it.get("detail"),
                             it.get("ttft_ms"), it.get("e2e_ms"),
                             it.get("prefill_tps"), it.get("decode_tps"),
                             it.get("status"), it.get("error") or "",
                             (it.get("answer") or "").replace("\n", " ")[:500]])
        from fastapi.responses import Response
        return Response(
            content="\ufeff" + buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="llm-monitor-eval-{run_id}.csv"'})
    return JSONResponse({"run": run, "items": items}, headers={
        "Content-Disposition": f'attachment; filename="llm-monitor-eval-{run_id}.json"'})


# --------------------------------------------------------------------------
# 历史数据与报告
# --------------------------------------------------------------------------
@app.get("/api/runs")
async def list_runs(
    model: str = "", type: str = "", date_from: str = "", date_to: str = "",
    limit: int = 100, offset: int = 0,
) -> Dict[str, Any]:
    runs = db.list_runs(model=model, run_type=type, date_from=date_from,
                        date_to=date_to, limit=limit, offset=offset)
    return {"runs": runs, "count": len(runs)}


@app.get("/api/runs/compare")
async def compare_runs(ids: str = Query(..., description="逗号分隔的 run id")) -> Dict[str, Any]:
    try:
        run_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(400, "ids 参数格式错误") from exc
    out = []
    for rid in run_ids[:6]:
        run = db.get_run(rid)
        if not run:
            continue
        details = db.get_run_details(rid)
        summary = tester.summarize(
            [{"status": d.get("status"), "ttft_ms": d.get("ttft_ms"),
              "e2e_ms": d.get("e2e_ms"), "tps": d.get("tps"),
              "prompt_tokens": d.get("prompt_tokens"),
              "prefill_ms": d.get("prefill_ms"),
              "completion_tokens": d.get("completion_tokens")} for d in details],
            run.get("duration_ms") or 0, run.get("concurrency") or 1)
        out.append({"run": run, **summary})
    return {"items": out}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int) -> Dict[str, Any]:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "测试记录不存在")
    details = db.get_run_details(run_id)
    summary = tester.summarize(
        [{"status": d.get("status"), "ttft_ms": d.get("ttft_ms"),
          "e2e_ms": d.get("e2e_ms"), "tps": d.get("tps"),
          "prompt_tokens": d.get("prompt_tokens"),
          "prefill_ms": d.get("prefill_ms"),
          "completion_tokens": d.get("completion_tokens")} for d in details],
        run.get("duration_ms") or 0, run.get("concurrency") or 1)
    for key in ("env_json", "config_json", "summary_json"):
        if run.get(key):
            try:
                run[key.replace("_json", "")] = json.loads(run[key])
            except (ValueError, TypeError):
                pass
    return {"run": run, "details": details, **summary}


@app.get("/api/runs/{run_id}/export")
async def export_run(run_id: int, fmt: str = "json") -> Any:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "测试记录不存在")
    details = db.get_run_details(run_id)
    if fmt.lower() == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["run_id", "worker_id", "case_label", "ttft_ms", "e2e_ms",
                         "prompt_tokens", "completion_tokens", "tps", "status", "error"])
        for d in details:
            writer.writerow([run_id, d.get("worker_id"), d.get("case_label"),
                             d.get("ttft_ms"), d.get("e2e_ms"), d.get("prompt_tokens"),
                             d.get("completion_tokens"), d.get("tps"), d.get("status"),
                             d.get("error") or ""])
        from fastapi.responses import Response
        return Response(
            content="\ufeff" + buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="llm-monitor-run-{run_id}.csv"'})
    payload = {"run": run, "details": details}
    return JSONResponse(payload, headers={
        "Content-Disposition": f'attachment; filename="llm-monitor-run-{run_id}.json"'})


@app.get("/api/runs/{run_id}/report")
async def run_report(run_id: int) -> Dict[str, Any]:
    """生成 Markdown 形式的测试报告文本。"""
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "测试记录不存在")
    details = db.get_run_details(run_id)
    env = {}
    try:
        env = json.loads(run.get("env_json") or "{}")
    except (ValueError, TypeError):
        pass
    summary_json = {}
    try:
        summary_json = json.loads(run.get("summary_json") or "{}")
    except (ValueError, TypeError):
        pass
    s = tester.summarize(details, run.get("duration_ms") or 0, run.get("concurrency") or 1)
    st = s["stats"]
    ev_summary = summary_json.get("eval") if isinstance(summary_json, dict) else None
    gpu_names = ", ".join(env.get("gpu_names") or []) or "未检测到"
    lines = [
        f"# 测试报告 · Run #{run_id}",
        "",
        f"- **测试类型**：{run.get('run_type')}",
        f"- **模型**：`{run.get('model')}`",
        f"- **连接档案**：{run.get('conn_name')}",
        f"- **开始时间**：{run.get('started_at')}",
        f"- **结束时间**：{run.get('finished_at')}",
        f"- **状态**：{run.get('status')}",
        f"- **并发数**：{run.get('concurrency')}",
        f"- **总耗时**：{(run.get('duration_ms') or 0) / 1000:.1f} s",
        "",
        "## 环境快照",
        "",
        f"- GPU：{gpu_names}（显存 {env.get('vram_total_mb')} MB"
        f"{'，**模拟数据**' if env.get('gpu_simulated') else ''}）",
        f"- CPU 逻辑核心：{env.get('cpu_cores')}",
        f"- 内存总量：{env.get('ram_total_mb')} MB",
        f"- 操作系统：{env.get('os')} / Python {env.get('python')}",
    ]
    if not ev_summary:
        # 评测类 run 的逐题明细在 eval_items 表里，request_details 为空，
        # 此时不能拿空 details 去算统计（会得到一屏 None），改用评测自带的效率字段。
        lines += [
            "",
            "## 核心指标",
            "",
            "| 指标 | 数值 |",
            "| --- | --- |",
            f"| 请求总数 | {st['total']}（成功 {st['ok']} / 失败 {st['errors']}）|",
            f"| 错误率 | {(st['error_rate'] or 0) * 100:.2f}% |",
            f"| 平均生成速度 | {st['avg_tps']} tokens/s |",
            f"| Prefill / Decode 吞吐 | {st.get('prefill_tps')} / {st.get('decode_tps')} tokens/s"
            f"{'' if st.get('prefill_tps') else '（非流式请求无法拆分）'} |",
            f"| 整体吞吐 | {st.get('throughput_tps')} tokens/s |",
            f"| 平均首 Token 延迟 | {st['avg_ttft_ms']} ms |",
            f"| P95 首 Token 延迟 | {st.get('p95_ttft_ms')} ms |",
            f"| 平均端到端延迟 | {st['avg_e2e_ms']} ms |",
            f"| P50 / P90 / P95 / P99 端到端 | {st['p50_e2e_ms']} / {st['p90_e2e_ms']} / "
            f"{st.get('p95_e2e_ms')} / {st['p99_e2e_ms']} ms |",
            f"| QPS | {st['qps']} |",
            f"| 生成 Token 总量 | {st['total_tokens']} |",
            f"| GPU 峰值利用率 | {run.get('gpu_peak_util')}% |",
            f"| VRAM 峰值 | {run.get('gpu_peak_vram')} MB |",
        ]
    rec = summary_json.get("recommend")
    if rec and rec.get("ok"):
        lines += ["", f"> **建议并发上限**：{rec.get('recommended')}（{rec.get('reason')}）"]
    stages = summary_json.get("stages")
    if stages:
        lines += ["", "## 阶梯压测明细", "",
                  "| 并发 | 请求数 | QPS | 平均TPS | 平均TTFT(ms) | P95(ms) | P99(ms) | "
                  "Prefill/Decode(tok/s) | 整体吞吐 | 错误率 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for s2 in stages:
            lines.append(
                f"| {s2['concurrency']} | {s2['total']} | {s2['qps']} | {s2['avg_tps']} | "
                f"{s2['avg_ttft_ms']} | {s2.get('p95_e2e_ms')} | {s2['p99_e2e_ms']} | "
                f"{s2.get('prefill_tps')}/{s2.get('decode_tps')} | "
                f"{s2.get('throughput_tps')} | "
                f"{(s2.get('error_rate') or 0) * 100:.1f}% |")
    cases = summary_json.get("cases")
    if cases:
        lines += ["", "## 套件用例明细", "",
                  "| 用例 | 类别 | TTFT(ms) | E2E(ms) | TPS | 状态 |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for c in cases:
            lines.append(
                f"| {c.get('label')} | {c.get('category')} | {c.get('ttft_ms')} | "
                f"{c.get('e2e_ms')} | {c.get('tps')} | {c.get('status')} |")
    if summary_json.get("stop_reason"):
        lines += ["", f"> 压测熔断原因：{summary_json['stop_reason']}"]
    ev = summary_json.get("eval")
    if ev:
        lines += ["", "## 评测结果", "",
                  f"- **套件**：{ev.get('suite_name')}（`{ev.get('suite_id')}`）",
                  f"- **类别**：{ev.get('category')} · **模式**：{ev.get('mode')}",
                  f"- **题量**：{ev.get('total')}（判分 {ev.get('graded')} / 失败 {ev.get('errors')}）"]
        label_map = {
            "accuracy": "准确率", "pass_at_1": "pass@1", "pass_at_k": "pass@k",
            "instruction_rate": "指令遵循率", "rougeL": "ROUGE-L", "f1": "F1",
            "refusal_rate": "有害请求拒绝率", "jailbreak_rate": "越狱成功率",
            "hallucination_rate": "幻觉率", "judge_score": "裁判评分（10 分制）",
            "score": "综合得分", "rubric_coverage": "评分要点覆盖率",
        }
        lines += ["", "| 指标 | 数值 |", "| --- | --- |"]
        for k, name in label_map.items():
            if ev.get(k) is not None:
                lines.append(f"| {name} | {ev[k]} |")
        lines += [
            f"| 平均首 Token 延迟 | {ev.get('avg_ttft_ms')} ms |",
            f"| 平均端到端延迟 | {ev.get('avg_e2e_ms')} ms |",
            f"| P95 端到端延迟 | {ev.get('p95_e2e_ms')} ms |",
            f"| Prefill / Decode 吞吐 | {ev.get('avg_prefill_tps')} / "
            f"{ev.get('avg_decode_tps')} tokens/s |",
            f"| 生成 Token 总量 | {ev.get('total_completion_tokens')} |",
        ]
        if ev.get("score_source"):
            lines.append(f"| 得分来源 | {ev['score_source']} |")
        if ev.get("by_length"):
            lines += ["", "### NIAH 按上下文长度命中率", "", "| 上下文长度 | 命中率 |",
                      "| --- | --- |"]
            for k, v in ev["by_length"].items():
                lines.append(f"| {k} | {v} |")
        if ev.get("constraint_failures"):
            lines += ["", "### 未通过的约束类型", ""]
            for k, v in ev["constraint_failures"].items():
                lines.append(f"- {k}：{v} 次")
        if ev.get("note"):
            lines += ["", f"> {ev['note']}"]
    lines += ["", "---", "",
              "由「本地大模型实时性能监控测试平台」自动生成。"]
    return {"markdown": "\n".join(lines), "run_id": run_id}


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: int) -> Dict[str, Any]:
    if not db.get_run(run_id):
        raise HTTPException(404, "测试记录不存在")
    db.delete_run(run_id)
    return {"ok": True}


# --------------------------------------------------------------------------
# 实时指标
# --------------------------------------------------------------------------
@app.get("/api/metrics/realtime")
async def metrics_realtime(seconds: float = 300, max_points: int = 900) -> Dict[str, Any]:
    return {
        "samples": db.query_samples(seconds, max_points),
        "latest": collector.last_sample,
        "stats": collector.stats.snapshot(),
    }


@app.get("/api/metrics/latest")
async def metrics_latest() -> Dict[str, Any]:
    return {"latest": collector.last_sample, "stats": collector.stats.snapshot()}


@app.get("/api/metrics/catalog")
async def metrics_catalog(conn_id: Optional[int] = None) -> Dict[str, Any]:
    """指标覆盖清单：四大类指标逐条列出取数来源与实现状态。"""
    conn = db.get_connection(conn_id) if conn_id else None
    return metric_catalog.catalog(conn)


@app.get("/api/metrics/efficiency")
async def metrics_efficiency(conn_id: int = Query(...), model: str = "") -> Dict[str, Any]:
    """效率指标：KV Cache 显存、参数量、量化、理论上限 FLOPs、PPL 支持情况等。

    数据来源优先级：llama-server 启动参数 → GGUF 头部 → 进程命令行，
    取不到的字段会明确返回 None 并给出原因，不做任何臆测填充。
    """
    conn = _require_conn(conn_id)
    try:
        return await probe.efficiency_info(conn, model or conn.get("default_model") or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("效率指标采集失败: %s", exc)
        raise HTTPException(502, f"效率指标采集失败: {exc}") from exc


@app.post("/api/metrics/cold-start")
async def metrics_cold_start(payload: ColdStartIn) -> Dict[str, Any]:
    """冷启动时间探测。部分后端（如常驻的 llama.cpp 服务）无法真正冷启，
    此时返回 proxy 指标并在 reason 里说明，绝不伪造数值。"""
    conn = _require_conn(payload.conn_id)
    try:
        return await probe.cold_start_probe(
            conn, payload.model or conn.get("default_model") or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("冷启动探测失败: %s", exc)
        raise HTTPException(502, f"冷启动探测失败: {exc}") from exc


# --------------------------------------------------------------------------
# 告警
# --------------------------------------------------------------------------
@app.get("/api/alerts")
async def get_alerts(limit: int = 100, acked: Optional[int] = None) -> Dict[str, Any]:
    return {"alerts": db.list_alerts(limit, acked), "rules": alerts.get_rules()}


@app.put("/api/alerts/rules")
async def put_alert_rules(rules: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return {"ok": True, "rules": alerts.save_rules(rules)}


@app.post("/api/alerts/ack")
async def ack_alerts(ids: Optional[List[int]] = Body(default=None)) -> Dict[str, Any]:
    db.ack_alerts(ids)
    return {"ok": True}


@app.delete("/api/alerts")
async def clear_alerts() -> Dict[str, Any]:
    db.clear_alerts()
    return {"ok": True}


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------
@app.get("/api/settings")
async def get_settings() -> Dict[str, Any]:
    return {"settings": db.get_setting("ui", {}) or {},
            "alert_rules": alerts.get_rules(),
            "server": {"sample_interval": config.SAMPLE_INTERVAL,
                       "retention_days": config.SAMPLE_RETENTION_DAYS,
                       "breaker": {"error_rate": config.BREAKER_ERROR_RATE,
                                   "vram_pct": config.BREAKER_VRAM_PCT}}}


@app.put("/api/settings")
async def put_settings(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    db.set_setting("ui", payload)
    return {"ok": True, "settings": payload}


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    await broadcaster.register(ws)
    try:
        await ws.send_text(json.dumps({
            "type": "hello",
            "data": {
                "latest": collector.last_sample,
                "stats": collector.stats.snapshot(),
                "active_runs": tester.list_active(),
                "sample_interval": config.SAMPLE_INTERVAL,
            },
        }, ensure_ascii=False, default=str))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "ping":
                await broadcaster.send_to(ws, {"type": "pong", "ts": time.time()})
            elif msg.get("type") == "metrics_history":
                seconds = float(msg.get("seconds", 300))
                await broadcaster.send_to(ws, {
                    "type": "metrics_history",
                    "data": db.query_samples(seconds, 900),
                })
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("WebSocket 异常: %s", exc)
    finally:
        await broadcaster.unregister(ws)


# --------------------------------------------------------------------------
# 静态前端（必须最后挂载，避免覆盖 /api 路由）
# --------------------------------------------------------------------------
if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True),
              name="frontend")
