"""真实后端「不限制输出长度」验证：证明 max_tokens<=0 时不会被悄悄砍在固定长度上。

背景（踩过的坑）：面板默认给了一个 1024 的 max_tokens，于是 llama.cpp 老老实实在
第 1024 个 token 处返回 ``stop_type=limit``，界面上看起来就是「刚输出一点就被截断」。
约定改成「默认不限制」之后，必须拿**真实后端**跑一次长回答来证明这条链路真的通了 ——
演示后端的绿色只说明代码路径通，不代表真实服务的参数映射正确。

用法::

    python tools/unlimited_probe.py                     # 默认连 127.0.0.1:8080 的 llama.cpp
    LLM_URL=http://127.0.0.1:8080 BACKEND=llama_cpp python tools/unlimited_probe.py
    APP_URL=http://127.0.0.1:8081 python tools/unlimited_probe.py

stdout 只输出纯 JSON（结论 + 关键数值），过程日志一律走 stderr。
不改动已有连接档案：同 base_url 的档案优先复用，没有才新建一条。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8099").rstrip("/")
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8080").rstrip("/")
BACKEND = os.environ.get("BACKEND", "llama_cpp")

PROMPT = ("请写一篇 1500 字以上的详细技术分析：本地部署大语言模型时，"
          "显存占用、上下文长度与生成吞吐三者之间如何互相牵制，"
          "并给出可落地的调优顺序与判据。要求分点展开，不要省略推理过程。")


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


async def ensure_conn(c: httpx.AsyncClient) -> dict:
    raw = (await c.get(f"{APP_URL}/api/connections")).json()
    conns = raw if isinstance(raw, list) else raw.get("connections", [])
    for conn in conns:
        if conn.get("backend") == BACKEND and LLM_URL in (conn.get("base_url") or ""):
            log(f"复用连接档案 #{conn['id']} {conn.get('name')}")
            return conn
    model = ""
    try:
        models = (await c.get(f"{LLM_URL}/v1/models", timeout=8)).json()
        model = (models.get("data") or [{}])[0].get("id", "")
    except Exception as exc:  # noqa: BLE001
        log(f"取模型列表失败（继续，用空模型名）：{exc}")
    r = await c.post(f"{APP_URL}/api/connections", json={
        "name": "不限制长度验证", "backend": BACKEND,
        "base_url": LLM_URL, "api_key": "", "default_model": model,
    })
    created = r.json()
    if not created.get("id"):
        raise RuntimeError(f"创建连接失败：{created}")
    log(f"新建连接档案 #{created['id']}（model={model or '未声明'}）")
    return {"id": created["id"], "default_model": model}


async def run_one(c: httpx.AsyncClient, conn: dict, session_id: int,
                  max_tokens: int, label: str) -> dict:
    payload = {
        "conn_id": conn["id"], "model": conn.get("default_model") or "",
        "prompt": PROMPT, "session_id": session_id, "mode": "chat",
        "echo_user": True, "max_tokens": max_tokens, "temperature": 0.7,
        "stream": True, "save": True,
    }
    out: dict = {"label": label, "max_tokens_sent": max_tokens, "chars": 0}
    async with c.stream("POST", f"{APP_URL}/api/test/single", json=payload,
                        timeout=httpx.Timeout(600.0, connect=10.0)) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            evt = json.loads(line[5:].strip())
            if evt.get("event") == "delta":
                out["chars"] += len(evt.get("text") or "")
            elif evt.get("event") == "metrics":
                d = evt["data"]
                out.update({
                    "completion_tokens": d.get("completion_tokens"),
                    "prompt_tokens": d.get("prompt_tokens"),
                    "finish_reason": d.get("finish_reason"),
                    "truncated": bool(d.get("truncated")),
                    "max_tokens_cfg": d.get("max_tokens_cfg"),
                    "ttft_ms": d.get("ttft_ms"),
                    "tps": d.get("tps"),
                    "e2e_ms": d.get("e2e_ms"),
                })
            elif evt.get("event") == "error":
                out["error"] = evt.get("message")
    return out


async def main() -> int:
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=60.0) as c:
        try:
            info = (await c.get(f"{APP_URL}/api/health", timeout=8)).json()
        except Exception as exc:  # noqa: BLE001
            log(f"连不上 {APP_URL}：{exc}")
            return 2
        log(f"平台 {APP_URL}（db={info.get('db')}）")
        log(f"目标后端 {BACKEND} → {LLM_URL}")
        try:
            conn = await ensure_conn(c)
            session = (await c.post(f"{APP_URL}/api/chat/sessions", json={
                "conn_id": conn["id"], "model": conn.get("default_model") or "",
                "title": "不限制长度验证",
            })).json()["session"]
        except Exception as exc:  # noqa: BLE001
            log(f"准备连接/会话失败：{exc}")
            return 2
        log(f"会话 #{session['id']}")

        log("① 不限制（max_tokens=0）→ 期望自然收尾，且长度不受 1024 之类的固定值限制")
        unlimited = await run_one(c, conn, session["id"], 0, "unlimited")
        log(f"   {json.dumps({k: v for k, v in unlimited.items() if k != 'label'}, ensure_ascii=False)}")

        # 这个会话只为验证而生，跑完就删掉，免得污染用户自己的会话列表
        try:
            await c.delete(f"{APP_URL}/api/chat/sessions/{session['id']}")
            log(f"已清理验证会话 #{session['id']}")
        except Exception as exc:  # noqa: BLE001
            log(f"清理验证会话失败（不影响结论）：{exc}")

    def check(cond: bool, name: str, detail: str = "") -> None:
        if cond:
            log(f"  PASS  {name}")
        else:
            failures.append(name + (f" << {detail}" if detail else ""))
            log(f"  FAIL  {name}{'  << ' + detail if detail else ''}")

    if unlimited.get("error"):
        check(False, "不限制请求未报错", str(unlimited["error"])[:200])
    else:
        ct = unlimited.get("completion_tokens") or 0
        check(unlimited.get("max_tokens_cfg") in (0, None),
              f"平台确实没有下发长度上限（max_tokens_cfg={unlimited.get('max_tokens_cfg')}）")
        check(not unlimited.get("truncated"),
              "未被标记为截断（truncated=False）",
              f"finish={unlimited.get('finish_reason')}")
        check(unlimited.get("finish_reason") == "stop",
              f"自然收尾（finish_reason={unlimited.get('finish_reason')}）",
              "说明模型是自己停的，不是被长度上限砍的")
        check(ct > 1024,
              f"输出长度突破 1024 token（实际 {ct} tokens）",
              "若恰好停在 1024，说明仍有一个固定上限在生效")
        check(not (ct and ct % 512 == 0 and ct <= 4096 and unlimited.get("truncated")),
              "没有停在 512 的整数倍这种典型上限值上", f"ct={ct}")

    log("② 结论")
    report = {
        "app_url": APP_URL, "llm_url": LLM_URL, "backend": BACKEND,
        "unlimited": unlimited, "assertionFailures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    log(f"\n断言失败：{len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
