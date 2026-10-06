"""端到端验证对话测试的「多轮记忆 + 不截断」。

覆盖用例
--------
A. 多轮记忆    —— 第二轮能引用第一轮的事实（问了什么 / 数字结论）
B. 长期记忆    —— System Prompt 里始终带记忆；记忆合并去重
C. 上下文裁剪  —— 超预算时按「整轮」丢弃、给模型裁剪提示、角色仍从 user 开始
D. 重新生成    —— 同一提问重发时不会出现两条相同 user 轮（dedup）
E. 继续生成    —— 续写指令不被回显，且确实承接上文
F. 不截断      —— 小 max_tokens 下也不会停在半个句子；不限制时给完整收尾
G. 持久化      —— 落库的轮数/累计 token 与消息表一致

用法：
    set LLM_MONITOR_DB=<临时库路径>
    python tools/chat_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "LLM_MONITOR_DB",
    str(Path(tempfile.gettempdir()) / "chat_smoke_run.db"))

from backend import chat, config, db, probe  # noqa: E402

PASS = "  PASS"
failures: list[str] = []


class Session(dict):
    """一个最小的「会话」替身，用于不落库的上下文装配测试。"""

    def __init__(self, **kw):
        super().__init__(**kw)


def check(cond: bool, name: str, detail: str = "") -> None:
    if cond:
        print(f"{PASS}  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name}" + (f"  << {detail}" if detail else ""))


async def ask(adapter, session, history, text, max_tokens=1024, budget=8192,
              mode="chat"):
    """按真实链路装配上下文并取一次回答。"""
    messages, info = chat.build_context(
        session, history, text, budget=budget,
        reserve=0 if not max_tokens or max_tokens <= 0 else max_tokens,
        regenerate=(mode == "regenerate"))
    res = await adapter.chat_collect("demo-qwen-7b", messages, stream=True,
                                     max_tokens=max_tokens)
    res["context"] = info
    res["finish_reason"] = probe.normalize_finish_reason(res["meta"])
    res["truncated"] = probe.is_truncated(res["meta"], res["completion_tokens"],
                                          max_tokens)
    return res


def _half_sentence_tail(text: str) -> bool:
    """粗判结尾是否是半句（以连接性标点收尾就认为被切了）。"""
    return text.rstrip().endswith(("，", "、", "：", "；", "（", "“", "…", "-", "——"))


async def main() -> int:
    db.init_db()
    print(f"库：{config.DB_PATH}\n")

    # 每个用例用独立会话，互不干扰
    def new_session(tag: str) -> dict:
        sid = db.create_chat_session(f"smoke-{tag}", None, "demo-qwen-7b")
        return db.get_chat_session(sid)

    conn = next((c for c in db.list_connections() if c.get("backend") == "demo"), None)
    if not conn:
        cid = db.add_connection("对话自测", "demo", "demo://local", "", "demo-qwen-7b")
        conn = db.get_connection(cid)
    adapter = probe.build_adapter(conn)

    # ---------------- A. 多轮记忆 ----------------
    print("A. 多轮记忆")
    sess = new_session("memory")
    r1 = await ask(adapter, sess, [], "17 × 23 等于多少？")
    check("391" in r1["text"], "第一轮算出正确结果 391",
          r1["text"][:60].replace("\n", " / "))

    history = [{"role": "user", "content": "17 × 23 等于多少？"},
               {"role": "assistant", "content": r1["text"]}]
    r2 = await ask(adapter, sess, history, "我刚才问的是什么？")
    check("17 × 23" in r2["text"], "第二轮能回忆起上一轮的提问",
          r2["text"][:70].replace("\n", " / "))

    history += [{"role": "user", "content": "我刚才问的是什么？"},
                {"role": "assistant", "content": r2["text"]}]
    r3 = await ask(adapter, sess, history, "那它的验算过程呢？")
    check("17 × 23" in r3["text"], "第三轮追问能承接上文（而非答非所问）",
          r3["text"][:70].replace("\n", " / "))
    # turns_loaded / turns_total 的口径都是「会话历史」轮数，不含正在生成的这一轮
    check(r3["context"]["turns_loaded"] == 2 and r3["context"]["turns_total"] == 2,
          f"历史轮次全部载入（{r3['context']['turns_loaded']}"
          f"/{r3['context']['turns_total']}）")
    check(r3["context"]["dropped_turns"] == 0, "预算充足时不误报裁剪")

    # ---------------- B. 长期记忆 ----------------
    print("\nB. 长期记忆")
    db.update_chat_session(sess["id"],
                           memory="- 用户在做本地大模型监控平台\n- 偏好中文回答")
    sess = db.get_chat_session(sess["id"])
    r4 = await ask(adapter, sess, history, "继续说说显存")
    sys_msg = ""
    messages, info4 = chat.build_context(
        sess, history, "继续说说显存", budget=8192, reserve=1024)
    sys_msg = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    check(chat.MEMORY_HEADER in sys_msg and "偏好中文回答" in sys_msg,
          "长期记忆始终拼进 System Prompt")
    check(info4["memory_chars"] > 0, "上下文明细里报告了记忆占用")

    merged, added = chat.merge_memory("- 用户在做本地大模型监控平台\n- 偏好中文回答",
                                     "- 偏好中文回答\n- 关注 KV Cache\n1. 其他")
    check("关注 KV Cache" in merged, "记忆合并：新增条目被并入")
    check(merged.count("偏好中文回答") == 1, "记忆合并：重复条目不重复写入")
    check("用户在做本地大模型监控平台" in merged, "记忆合并：原有条目被保留")

    # ---------------- C. 上下文裁剪 ----------------
    print("\nC. 上下文裁剪")
    big: list[dict] = []
    for i in range(40):
        big.append({"role": "user", "content": f"第 {i} 轮提问：" + "内容" * 60})
        big.append({"role": "assistant", "content": "回答" * 80})
    msgs_c, info_c = chat.build_context(Session(), big, "继续", budget=2048,
                                        reserve=256)
    check(info_c["dropped_turns"] > 0,
          f"超预算时丢弃更早的轮次（dropped={info_c['dropped_turns']}）")
    check(info_c["turn_tokens"] <= 2048 + 200,
          f"载入 token 未超预算（turn_tokens={info_c['turn_tokens']}）")
    check("已因长度上限被省略" in (msgs_c[0]["content"] if msgs_c else ""),
          "给模型下发了裁剪提示（它会如实说明而不是瞎编）")
    check(msgs_c[1]["role"] == "user", "裁剪后首条仍是 user（避免凭空回答）")
    # 角色必须交替
    roles = [m["role"] for m in msgs_c[1:]]
    check(all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)),
          "消息角色严格交替")

    # ---------------- D. 重新生成去重 ----------------
    print("\nD. 重新生成（同一提问重发）")
    hist_dup = [{"role": "user", "content": "17 × 23 等于多少？"},
                {"role": "assistant", "content": "结果是 391。"},
                {"role": "user", "content": "详细讲讲并发与排队"}]
    msgs_d, info_d = chat.build_context(Session(), hist_dup, "详细讲讲并发与排队",
                                        budget=8192, reserve=300)
    users_d = [m["content"] for m in msgs_d if m["role"] == "user"]
    check(info_d["dedup_user"] is True, "识别出「历史末尾与本轮提问相同」")
    check(users_d.count("详细讲讲并发与排队") == 1,
          "同一提问只出现一次（不产生两条连续相同 user 轮）")
    check(info_d["dropped_turns"] == 0, "去重不误报为「被裁剪」")
    r_d = await ask(adapter, Session(), hist_dup, "详细讲讲并发与排队", max_tokens=400)
    check("你之前问的「详细讲讲并发与排队」" not in r_d["text"],
          "模型不会把本轮提问当成「上一轮」",
          r_d["text"].split("\n")[0][:60])

    # 历史以 assistant 结尾（点「继续生成」出来的片段上再点「重新生成」时的真实形态）：
    # 上下文必须截到最后一条 user 提问，否则模型看到的是「已答完又问一遍」
    hist_r = [{"role": "user", "content": "17 × 23 等于多少？"},
              {"role": "assistant", "content": "结果是 391。"},
              {"role": "user", "content": "我刚才问的是什么？"},
              {"role": "assistant", "content": "你上一轮问的是「17 × 23 等于多少？」。"}]
    msgs_r, info_r = chat.build_context(Session(), hist_r, "我刚才问的是什么？",
                                        budget=8192, reserve=300, regenerate=True)
    # 期望：末尾那条旧回答被丢掉、重复的提问被去重，最终以提问结尾
    check([m["role"] for m in msgs_r] == ["user", "assistant", "user"],
          f"重新生成时上下文以提问结尾（{','.join(m['role'] for m in msgs_r)}）")
    check(info_r["regenerate"] is True, "上下文明细里标注了 regenerate")
    r_r = await ask(adapter, Session(), hist_r, "我刚才问的是什么？",
                    max_tokens=300, mode="regenerate")
    check("17 × 23" in r_r["text"],
          "重新生成时上文回溯到更早那一轮",
          r_r["text"].split("\n")[0][:70])
    check("你上一轮问的是：「我刚才问的是什么？」" not in r_r["text"],
          "「上一轮」不会误指向本轮自身",
          r_r["text"].split("\n")[0][:70])

    # ---------------- E. 继续生成 ----------------
    print("\nE. 继续生成")
    hist_e = [{"role": "user", "content": "介绍一下 KV Cache"},
              {"role": "assistant", "content": "KV Cache 是注意力机制的缓存……"}]
    r_e = await ask(adapter, Session(), hist_e, probe.CONTINUE_INSTRUCTION,
                    max_tokens=600)
    check(probe.CONTINUE_INSTRUCTION[:10] not in r_e["text"],
          "续写回答里不回显指令原文")
    check("接上文续写" in r_e["text"] or "补充" in r_e["text"],
          "续写确实承接上文（而非从头发散）",
          r_e["text"][:60].replace("\n", " / "))

    # ---------------- F. 不截断 ----------------
    print("\nF. 不截断")
    r_f1 = await ask(adapter, Session(), [], "详细讲讲并发与排队", max_tokens=100)
    check(not _half_sentence_tail(r_f1["text"]),
          "小 max_tokens=100 时刻在句子边界，不出半句",
          repr(r_f1["text"][-28:]))
    check(r_f1["finish_reason"] == "length",
          f"如实标注终止原因为 length（got={r_f1['finish_reason']}）")
    check(r_f1["truncated"] is True, "truncated 标记为真（前端可提示「继续生成」）")

    r_f2 = await ask(adapter, Session(), [], "介绍一下 KV Cache", max_tokens=0)
    check(not _half_sentence_tail(r_f2["text"]),
          "不限制模式（max_tokens=0）结尾完整",
          repr(r_f2["text"][-28:]))
    check(r_f2["finish_reason"] == "stop",
          f"不限制模式终止原因为 stop（got={r_f2['finish_reason']}）")
    check(r_f2["truncated"] is False, "不限制模式下 truncated 为假")
    check(r_f2["completion_tokens"] > 100,
          f"不限制模式给出足够长的完整回答（{r_f2['completion_tokens']} tokens）")

    # ---------------- G. 持久化 ----------------
    print("\nG. 持久化与统计")
    sess_g = new_session("persist")
    r_g1 = await ask(adapter, sess_g, [], "第一问：显存怎么算？")
    chat.record_turn(sess_g["id"], "第一问：显存怎么算？", r_g1["text"],
                     continued=False, truncated=r_g1["truncated"],
                     finish_reason=r_g1["finish_reason"],
                     prompt_tokens=r_g1["prompt_tokens"],
                     completion_tokens=r_g1["completion_tokens"])
    r_g2 = await ask(adapter, sess_g, [], "第二问：并发怎么压？")
    chat.record_turn(sess_g["id"], "第二问：并发怎么压？", r_g2["text"],
                     continued=False, truncated=r_g2["truncated"],
                     finish_reason=r_g2["finish_reason"])
    row = db.get_chat_session(sess_g["id"])
    msgs_g = db.list_chat_messages(sess_g["id"])
    check(len(msgs_g) == 4, f"两条问答共落库 4 条消息（got={len(msgs_g)}）")
    check(row["total_turns"] == 2, f"会话轮数为 2（got={row['total_turns']}）")
    check(row["total_tokens"] > 0, f"累计 token 已统计（{row['total_tokens']}）")
    hist_g = chat.load_history(sess_g["id"])
    check([m["role"] for m in hist_g] == ["user", "assistant", "user", "assistant"],
          "重新读取的历史角色序列正确（重启后仍在）")

    print()
    if failures:
        print(f"合计失败用例：{len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("合计失败用例：0")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
