"""端到端跑一遍全部评测套件（demo 后端，最快模式），验证 evaluator 判分与落库链路。

用法：
    set LLM_MONITOR_DB=<临时库路径>
    python tools/eval_smoke.py
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
    str(Path(tempfile.gettempdir()) / "eval_smoke_run.db"))

from backend import collector, config, db, evaluator, probe  # noqa: E402


async def main() -> int:
    db.init_db()
    conn = next((c for c in db.list_connections() if c.get("backend") == "demo"), None)
    if not conn:
        cid = db.add_connection("评测自测", "demo", "demo://local", "", "demo-qwen-7b")
        conn = db.get_connection(cid)
    adapter = probe.build_adapter(conn)
    model = conn.get("default_model") or "demo-qwen-7b"

    suites = [s for s in evaluator.load_suites()]
    print(f"共 {len(suites)} 个套件，库：{config.DB_PATH}\n")
    header = f"{'套件':<18}{'指标':<17}{'题数':>5}{'判分':>5}{'状态':>6}  关键结果"
    print(header)
    print("-" * len(header) * 2)

    failures = 0
    for suite in suites:
        mode = "quick"
        cfg = {"suite_id": suite["id"], "mode": mode,
               "allow_code_exec": True, "overrides": {}}
        run_id = db.create_run(conn["id"], conn.get("name", ""), model, "eval",
                               suite["id"], 1, {}, cfg)
        try:
            payload = await evaluator.run_eval(run_id, conn, adapter, model, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"{suite['id']:<18}{suite['metric']:<17}{'-':>5}{'-':>5}{'异常':>6}  "
                  f"{type(exc).__name__}: {exc}")
            failures += 1
            continue
        ev = payload["eval"]
        graded = ev.get("graded", 0)
        status = "OK" if graded else "未判分"
        if not graded:
            failures += 1
        keys = [k for k in ("accuracy", "pass_at_1", "pass_at_k", "instruction_rate",
                            "rougeL", "f1", "refusal_rate", "jailbreak_rate",
                            "judge_score", "score", "hallucination_rate")
                if ev.get(k) is not None]
        detail = ", ".join(f"{k}={ev[k]}" for k in keys)
        if ev.get("by_length"):
            detail += " by_len=" + str(ev["by_length"])
        print(f"{suite['id']:<18}{suite['metric']:<17}{ev.get('total', 0):>5}"
              f"{graded:>5}{status:>6}  {detail}")
        items = db.get_eval_items(run_id)
        assert len(items) == ev.get("total", 0), \
            f"{suite['id']}: 落库题数 {len(items)} != 预期 {ev.get('total')}"

    print()
    print(f"合计失败套件：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
