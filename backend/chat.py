"""对话测试的「记忆」层。

职责
----
1. **短期记忆（上下文）** —— 每次请求都把该会话的历史消息按顺序装进
   ``messages``，模型因此能看到之前说过的话。装之前会按上下文预算裁剪，
   保证不会把模型上下文撑爆；裁剪结果（载入了多少轮 / 丢了多少轮 /
   占了多少 token）会一并回给前端，让「记忆」是可见、可核对的。
2. **长期记忆** —— 会话级 ``memory`` 字段。用户可手动编辑，也可以让模型
   从当前对话里提炼。它始终拼进 System Prompt，因此不受轮次裁剪影响。
3. **落库** —— 每轮问答写入 ``chat_messages``，刷新页面/重启服务后对话仍在。

注意：这里**只做装配与记账**，不产生任何模型输出；所有数值都来自真实消息
或真实分词估算，不做模拟。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config
from . import db
from .probe import CONTINUE_INSTRUCTION, BaseAdapter, estimate_tokens

log = logging.getLogger("llm-monitor.chat")

# 每条消息在 prompt 里还有 role 标记等固定开销，按 4 token 估
_MSG_OVERHEAD = 4

MEMORY_HEADER = (
    "【长期记忆】以下是此前沉淀下来的、需要你始终遵守的背景与约定，"
    "请在后续所有回答中沿用，不要与它们矛盾："
)
NOTE_DROPPED = (
    "（上下文提示：更早的 {dropped} 轮对话已因长度上限被省略，"
    "你目前只能看到最近 {kept} 轮。若用户问及更早的内容，请如实说明这部分已被裁剪。）"
)

DISTILL_SYSTEM = (
    "你是一个对话记忆整理助手。请从用户提供的对话记录中提炼出**值得长期记住**的信息，"
    "用于后续对话复用。要求：\n"
    "1. 只保留关于用户的稳定事实、偏好、约定，以及当前任务的目标与结论；\n"
    "2. 忽略寒暄、客套与一次性的中间过程；\n"
    "3. 每条一行，以「- 」开头，不超过 40 字，最多 8 条；\n"
    "4. 只输出这些条目本身，不要任何前言、解释或标题。"
)

MAX_TITLE_CHARS = 24


# --------------------------------------------------------------------------
# System Prompt 装配
# --------------------------------------------------------------------------
def compose_system(session: Optional[Dict[str, Any]], override: str = "",
                   use_memory: bool = True) -> str:
    """拼出本次请求的 System Prompt：会话 System + 长期记忆。"""
    parts: List[str] = []
    base = (override or "").strip()
    if not base and session:
        base = (session.get("system") or "").strip()
    if base:
        parts.append(base)
    memory = (session.get("memory") or "").strip() if (session and use_memory) else ""
    if memory:
        parts.append(f"{MEMORY_HEADER}\n{memory}")
    return "\n\n".join(parts)


def load_history(session_id: int) -> List[Dict[str, str]]:
    """读取会话的全部历史消息（不含 system），按时间正序。"""
    rows = db.list_chat_messages(session_id)
    return [{"role": r["role"], "content": r["content"] or ""} for r in rows
            if r.get("role") in ("user", "assistant")]


def _clip_message(text: str, limit_chars: int) -> Tuple[str, bool]:
    """单条消息过长时保留尾部（对回答更有用）并加省略标记。"""
    if limit_chars <= 0 or len(text) <= limit_chars:
        return text, False
    head_keep = max(0, limit_chars // 5)
    tail_keep = limit_chars - head_keep
    return (text[:head_keep] + "\n……（中间内容因单条消息过长已省略）……\n"
            + text[-tail_keep:]), True


def build_context(session: Optional[Dict[str, Any]],
                  history: Sequence[Dict[str, str]],
                  user_text: str,
                  system_override: str = "",
                  budget: Optional[int] = None,
                  reserve: int = 0,
                  keep_turns: Optional[int] = None,
                  use_memory: bool = True,
                  regenerate: bool = False) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """装配本次请求的 messages，并返回上下文占用明细。

    返回 ``(messages, info)``，其中 ``info`` 形如::

        {"budget": 8192, "reserve": 1024, "system_tokens": 210,
         "history_tokens": 640, "prompt_tokens_est": 812, "turn_tokens": 812,
         "turns_loaded": 4, "turns_total": 9, "dropped_turns": 5,
         "msg_chars_clipped": 0, "memory_used": True, "budget_source": "默认"}
    """
    sess = session or {}
    budget_source = "默认"
    if budget is None:
        budget = sess.get("ctx_budget")
        if budget:
            budget_source = "会话设置"
    if not budget or budget <= 0:
        budget = config.CHAT_CTX_BUDGET
        budget_source = "默认"
    budget = int(budget)

    if keep_turns is None:
        keep_turns = sess.get("keep_turns")
    if not keep_turns or keep_turns <= 0:
        keep_turns = config.CHAT_KEEP_TURNS
    keep_turns = int(keep_turns)

    sys_text = compose_system(sess, system_override, use_memory=use_memory)
    system_tokens = (estimate_tokens(sys_text) + _MSG_OVERHEAD) if sys_text else 0
    prompt_tokens = (estimate_tokens(user_text) + _MSG_OVERHEAD) if user_text else 0

    # 可用来放历史的额度 = 预算 - 输出预留 - system - 本轮提问
    avail = max(0, budget - int(reserve or 0) - system_tokens - prompt_tokens)

    kept_rev: List[Dict[str, str]] = []
    used = 0
    clipped = 0
    turns_loaded = 0
    # 保证至少保留最近 keep_turns 轮（1 轮 = user + assistant）
    soft_floor_msgs = keep_turns * 2

    for idx, msg in enumerate(reversed(list(history))):
        text = msg.get("content") or ""
        text, was_clipped = _clip_message(text, config.CHAT_MAX_MSG_CHARS)
        if was_clipped:
            clipped += 1
        cost = estimate_tokens(text) + _MSG_OVERHEAD
        if used + cost > avail and len(kept_rev) >= soft_floor_msgs:
            break
        kept_rev.append({"role": msg.get("role", "user"), "content": text})
        used += cost

    kept = list(reversed(kept_rev))

    # 重新生成：上下文必须以「提问」结尾，而不是「提问 + 一条旧回答」。
    # 真实推理服务重答也是这样做的 —— 截断到最后一条 user 消息再生成一次，
    # 否则模型看到的是「已经回答过这个问题了，后面又莫名重复一遍」。
    if regenerate:
        while kept and kept[-1]["role"] == "assistant":
            tail = kept.pop()
            used = max(0, used - estimate_tokens(tail.get("content") or "") - _MSG_OVERHEAD)

    # 同一提问被再次发出时（重新生成、或用户手动重发同一句），历史末尾已经有
    # 一条一模一样的 user 消息。若再追加一条，就会出现两条连续且内容相同的
    # user 轮：既白占上下文，也会让模型以为「用户又问了一遍同样的问题」
    #（演示后端曾据此回答「我结合你之前问的『本轮的这个问题』」）。
    # 这里把末尾那条去掉，用本轮的 user_text 顶替它的位置 —— 内容不变，只出现一次。
    dedup_user = False
    if user_text and kept and kept[-1]["role"] == "user" \
            and (kept[-1].get("content") or "").strip() == user_text.strip():
        prior = kept.pop()
        used = max(0, used - estimate_tokens(prior.get("content") or "") - _MSG_OVERHEAD)
        dedup_user = True

    turns_total = _count_turns(history)
    # 被顶替的那条仍是「本轮」，算作已载入，避免误报成「被裁剪」
    turns_loaded = _count_turns(kept) + (1 if dedup_user else 0)
    dropped = max(0, turns_total - turns_loaded)

    extra_note = ""
    if dropped:
        extra_note = NOTE_DROPPED.format(dropped=dropped, kept=turns_loaded)
        if sys_text:
            sys_text = f"{sys_text}\n\n{extra_note}"
        else:
            sys_text = extra_note
        system_tokens = estimate_tokens(sys_text) + _MSG_OVERHEAD

    # 消息序列：user/assistant 必须交替（部分后端对连续同角色不友好），
    # 裁剪后若首条是 assistant，去掉它，避免出现「凭空回答」。
    while kept and kept[0]["role"] != "user":
        kept.pop(0)

    messages: List[Dict[str, str]] = []
    if sys_text:
        messages.append({"role": "system", "content": sys_text})
    messages.extend(kept)
    if user_text:
        messages.append({"role": "user", "content": user_text})

    info = {
        "budget": budget,
        "budget_source": budget_source,
        "reserve": int(reserve or 0),
        "keep_turns": keep_turns,
        "system_tokens": system_tokens,
        "history_tokens": used,
        "prompt_tokens_est": prompt_tokens,
        "turn_tokens": system_tokens + used + prompt_tokens,
        "turns_loaded": turns_loaded,
        "turns_total": turns_total,
        "dropped_turns": dropped,
        "msg_chars_clipped": clipped,
        "dedup_user": dedup_user,
        "regenerate": bool(regenerate),
        "memory_used": bool((sess.get("memory") or "").strip()) and use_memory,
        "memory_chars": len((sess.get("memory") or "").strip()),
        "dropped_note": extra_note,
    }
    return messages, info


def _count_turns(messages: Sequence[Dict[str, str]]) -> int:
    """轮数 = user 消息条数（assistant 是对它的回答）。"""
    return sum(1 for m in messages if m.get("role") == "user")


# --------------------------------------------------------------------------
# 落库
# --------------------------------------------------------------------------
def record_turn(session_id: int, user_text: str, answer: str,
                run_id: Optional[int] = None, echo_user: bool = True,
                continued: bool = False, truncated: bool = False,
                finish_reason: str = "", prompt_tokens: Optional[int] = None,
                completion_tokens: Optional[int] = None,
                metrics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把一轮问答写入会话，并返回新插入的消息行。"""
    out: Dict[str, Any] = {"user_id": None, "assistant_id": None}
    if echo_user and user_text:
        out["user_id"] = db.add_chat_message(
            session_id, "user", user_text,
            prompt_tokens=None, completion_tokens=None,
            truncated=False, metrics={"context": (metrics or {}).get("context")},
        )
    if answer is not None:
        out["assistant_id"] = db.add_chat_message(
            session_id, "assistant", answer, run_id=run_id,
            continued=continued, truncated=truncated,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            metrics=metrics or {},
        )
    db.refresh_chat_session_stats(session_id)
    return out


def _auto_title(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return (t[:MAX_TITLE_CHARS] + "…") if len(t) > MAX_TITLE_CHARS else (t or "新会话")


def ensure_title(session: Dict[str, Any], first_user_text: str) -> None:
    """会话还叫默认名时，用第一条提问自动命名（方便在列表里辨认）。"""
    if not session:
        return
    title = (session.get("title") or "").strip()
    if title in ("", "新会话") and first_user_text:
        db.update_chat_session(session["id"], title=_auto_title(first_user_text))


# --------------------------------------------------------------------------
# 长期记忆：从对话中提炼
# --------------------------------------------------------------------------
def _memory_lines(memory: str) -> List[str]:
    return [ln.strip() for ln in (memory or "").splitlines() if ln.strip()]


def merge_memory(old: str, new: str, limit: int = 40) -> Tuple[str, List[str]]:
    """把提炼结果并入已有记忆：去重、保序、限制条数。返回 (新记忆, 新增条目)。"""
    existing = _memory_lines(old)
    seen = {ln.lstrip("-• ").strip() for ln in existing}
    added: List[str] = []
    for ln in _memory_lines(new):
        line = ln if ln.startswith("-") else f"- {ln.lstrip('• ·*')}"
        key = line.lstrip("-• ").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        existing.append(line)
        added.append(line)
    if len(existing) > limit:
        existing = existing[-limit:]
    return "\n".join(existing), added


async def distill_memory(adapter: BaseAdapter, model: str,
                         session: Dict[str, Any], history: Sequence[Dict[str, str]],
                         max_chars: int = 6000) -> Dict[str, Any]:
    """让模型把当前对话压缩成长期记忆条目。

    真实后端走真实推理；demo 后端走其模拟输出。取不到内容时如实返回失败原因。
    """
    if not history:
        return {"ok": False, "reason": "当前会话还没有对话内容，无法提炼记忆。",
                "memory": session.get("memory") or "", "added": []}

    lines: List[str] = []
    budget = max_chars
    for m in reversed(list(history)):
        chunk = f"{'用户' if m['role'] == 'user' else '助手'}：{m['content']}"
        if len(chunk) > budget:
            chunk = chunk[:budget]
        lines.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    transcript = "\n\n".join(reversed(lines))

    msgs = [{"role": "system", "content": DISTILL_SYSTEM},
            {"role": "user", "content": f"对话记录如下：\n\n{transcript}"}]
    try:
        res = await adapter.chat_collect(model, msgs, temperature=0.2,
                                         top_p=0.9, max_tokens=400, stream=False)
        text = (res.get("text") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("记忆提炼失败: %s", exc)
        return {"ok": False, "reason": f"调用模型失败：{exc}",
                "memory": session.get("memory") or "", "added": []}

    if not text:
        return {"ok": False, "reason": "模型没有返回可用的记忆条目。",
                "memory": session.get("memory") or "", "added": []}

    merged, added = merge_memory(session.get("memory") or "", text)
    if not added:
        return {"ok": True, "reason": "提炼完成，没有发现新的可记忆要点。",
                "memory": merged, "added": [], "raw": text}
    db.update_chat_session(session["id"], memory=merged)
    return {"ok": True, "reason": f"新增 {len(added)} 条记忆。",
            "memory": merged, "added": added, "raw": text}


# --------------------------------------------------------------------------
# 对外序列化
# --------------------------------------------------------------------------
def session_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "title": row.get("title") or "新会话",
        "conn_id": row.get("conn_id"),
        "model": row.get("model") or "",
        "system": row.get("system") or "",
        "memory": row.get("memory") or "",
        "ctx_budget": row.get("ctx_budget"),
        "keep_turns": row.get("keep_turns"),
        "total_turns": row.get("total_turns") or 0,
        "total_tokens": row.get("total_tokens") or 0,
        "turn_count": row.get("turn_count"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def message_public(row: Dict[str, Any]) -> Dict[str, Any]:
    import json as _json
    try:
        metrics = _json.loads(row.get("metrics_json") or "{}")
    except ValueError:
        metrics = {}
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "role": row.get("role"),
        "content": row.get("content") or "",
        "run_id": row.get("run_id"),
        "continued": bool(row.get("continued")),
        "truncated": bool(row.get("truncated")),
        "finish_reason": row.get("finish_reason") or "",
        "prompt_tokens": row.get("prompt_tokens"),
        "completion_tokens": row.get("completion_tokens"),
        "metrics": metrics,
        "created_at": row.get("created_at"),
    }
