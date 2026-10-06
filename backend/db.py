"""SQLite 存储层：DDL、连接管理与 CRUD。

设计要点
--------
* SQLite 单写者模型 —— 使用单条长连接 + 可重入锁串行化写入，
  并开启 WAL 以允许读写并发；批量插入走 ``executemany`` 单次提交。
* 所有时间戳统一用 unix 秒（float）或 ISO 文本，便于前端直接解析。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import config

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

# --------------------------------------------------------------------------
# DDL
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  backend       TEXT NOT NULL,              -- ollama / vllm / llama_cpp / openai_compat / demo
  base_url      TEXT NOT NULL,
  api_key       TEXT DEFAULT '',
  default_model TEXT DEFAULT '',
  last_status   TEXT DEFAULT 'unknown',     -- online / offline / timeout / unknown
  last_version  TEXT DEFAULT '',
  last_checked  REAL,
  created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS test_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  conn_id       INTEGER REFERENCES connections(id) ON DELETE SET NULL,
  conn_name     TEXT,
  model         TEXT NOT NULL,
  run_type      TEXT NOT NULL,              -- single / benchmark / loadtest
  prompt_label  TEXT,
  concurrency   INTEGER DEFAULT 1,
  status        TEXT DEFAULT 'running',     -- running / done / stopped / error
  started_at    TEXT,
  finished_at   TEXT,
  duration_ms   REAL,
  total_reqs    INTEGER DEFAULT 0,
  ok_reqs       INTEGER DEFAULT 0,
  avg_tps       REAL,
  avg_ttft_ms   REAL,
  p50_e2e_ms    REAL,
  p90_e2e_ms    REAL,
  p99_e2e_ms    REAL,
  avg_e2e_ms    REAL,
  p95_e2e_ms    REAL,
  p95_ttft_ms   REAL,
  prefill_tps   REAL,                       -- Prefill 段吞吐（算力密集）
  decode_tps    REAL,                       -- Decode 段吞吐（访存瓶颈）
  throughput_tps REAL,                      -- 整体吞吐：token 总量 / 墙钟时长
  rec_concurrency INTEGER,                  -- 建议并发上限（阶梯压测拐点）
  max_qps       REAL,
  error_count   INTEGER DEFAULT 0,
  error_rate    REAL,
  gpu_peak_vram REAL,
  gpu_peak_util REAL,
  gpu_avg_util  REAL,
  env_json      TEXT,                       -- 环境快照（GPU 型号/显存/量化级别等）
  config_json   TEXT,                       -- 本次测试的参数配置
  summary_json  TEXT                        -- 套件分项 / 阶梯结果等结构化明细
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON test_runs(started_at DESC);
CREATE TABLE IF NOT EXISTS request_details (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id            INTEGER REFERENCES test_runs(id) ON DELETE CASCADE,
  worker_id         INTEGER DEFAULT 0,
  case_label        TEXT,
  ttft_ms           REAL,
  e2e_ms            REAL,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  tps               REAL,
  prefill_ms        REAL,                   -- 后端自报的 Prompt 处理耗时（效率指标拆分用）
  status            TEXT,                   -- ok / timeout / error
  error             TEXT,
  created_at        TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_details_run ON request_details(run_id);

CREATE TABLE IF NOT EXISTS eval_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       INTEGER REFERENCES test_runs(id) ON DELETE CASCADE,
  item_id      TEXT,
  case_label   TEXT,
  prompt       TEXT,
  answer       TEXT,
  gold         TEXT,
  ok           INTEGER,
  metric       TEXT,
  detail       TEXT,
  grade_json   TEXT,                        -- 判分明细（选项/约束/ROUGE/裁判分数等）
  ttft_ms      REAL,
  e2e_ms       REAL,
  prefill_tps  REAL,
  decode_tps   REAL,
  status       TEXT,
  error        TEXT,
  created_at   TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_eval_items_run ON eval_items(run_id);

CREATE TABLE IF NOT EXISTS metric_samples (
  ts            REAL PRIMARY KEY,
  gpu_util      REAL,
  vram_used_mb  REAL,
  vram_total_mb REAL,
  gpu_temp      REAL,
  cpu_percent   REAL,
  ram_percent   REAL,
  ram_used_mb   REAL,
  proc_rss_mb   REAL,
  active_reqs   INTEGER,
  rps           REAL,
  current_tps   REAL,
  current_ttft  REAL,
  gpu_name      TEXT,
  simulated     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL,
  rule_key    TEXT,
  metric      TEXT,
  value       REAL,
  threshold   REAL,
  op          TEXT,
  level       TEXT,
  message     TEXT,
  acked       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- 对话会话：一次「对话测试」= 一个会话，多轮消息挂在下面。
-- 持久化的意义是「记忆」——刷新页面/重启服务后，上下文与长期记忆都还在。
CREATE TABLE IF NOT EXISTS chat_sessions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL DEFAULT '新会话',
  conn_id       INTEGER REFERENCES connections(id) ON DELETE SET NULL,
  model         TEXT DEFAULT '',
  system        TEXT DEFAULT '',            -- 会话级 System Prompt
  memory        TEXT DEFAULT '',            -- 长期记忆（用户可编辑 / 可从对话提炼）
  ctx_budget    INTEGER,                    -- 上下文预算（token），空则用全局默认
  keep_turns    INTEGER,                    -- 至少保留的最近轮数
  total_turns   INTEGER DEFAULT 0,
  total_tokens  INTEGER DEFAULT 0,
  created_at    TEXT DEFAULT (datetime('now','localtime')),
  updated_at    TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated ON chat_sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id        INTEGER REFERENCES chat_sessions(id) ON DELETE CASCADE,
  role              TEXT NOT NULL,          -- user / assistant
  content           TEXT NOT NULL,
  run_id            INTEGER,                -- 对应的 test_runs.id（性能指标在那边）
  continued         INTEGER DEFAULT 0,      -- 是否是「继续生成」拼上来的片段
  truncated         INTEGER DEFAULT 0,      -- 该轮是否因 max_tokens 触顶被截断
  finish_reason     TEXT,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  metrics_json      TEXT,
  created_at        TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id);
"""

SEED_CONNECTION = dict(
    name="内置演示模型（无需真实推理服务）",
    backend="demo",
    base_url="demo://local",
    api_key="",
    default_model="demo-qwen-7b",
)


# --------------------------------------------------------------------------
# 连接管理
# --------------------------------------------------------------------------
def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def _migrate(conn: Any) -> None:
    """轻量列迁移：对已存在的旧库补齐后加的字段（幂等）。"""
    wanted = {
        "request_details": {"prefill_ms": "REAL"},
        "test_runs": {
            "p95_e2e_ms": "REAL", "p95_ttft_ms": "REAL",
            "prefill_tps": "REAL", "decode_tps": "REAL",
            "throughput_tps": "REAL", "rec_concurrency": "INTEGER",
        },
    }
    for table, cols in wanted.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    with _lock:
        conn = get_conn()
        conn.executescript(SCHEMA)
        _migrate(conn)
        # 上次进程被强杀时可能残留 running 状态的记录，启动时统一标记为中断
        conn.execute(
            "UPDATE test_runs SET status='interrupted',"
            " finished_at=COALESCE(finished_at, datetime('now','localtime'))"
            " WHERE status='running'"
        )
        conn.commit()
        # 首次启动时种入一个演示连接，保证开箱即可看到完整功能
        cur = conn.execute("SELECT COUNT(*) AS c FROM connections")
        if cur.fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO connections (name, backend, base_url, api_key, default_model)"
                " VALUES (:name, :backend, :base_url, :api_key, :default_model)",
                SEED_CONNECTION,
            )
            conn.commit()


def _rows(sql: str, args: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    with _lock:
        cur = get_conn().execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def _row(sql: str, args: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    rows = _rows(sql, args)
    return rows[0] if rows else None


def _exec(sql: str, args: Sequence[Any] = ()) -> int:
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


def _exec_many(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    with _lock:
        conn = get_conn()
        conn.executemany(sql, seq)
        conn.commit()


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------
def list_connections() -> List[Dict[str, Any]]:
    return _rows("SELECT * FROM connections ORDER BY id")


def get_connection(conn_id: int) -> Optional[Dict[str, Any]]:
    return _row("SELECT * FROM connections WHERE id=?", (conn_id,))


def add_connection(name: str, backend: str, base_url: str, api_key: str = "",
                   default_model: str = "") -> int:
    return _exec(
        "INSERT INTO connections (name, backend, base_url, api_key, default_model)"
        " VALUES (?,?,?,?,?)",
        (name, backend, base_url, api_key, default_model),
    )


def update_connection(conn_id: int, **fields: Any) -> None:
    allowed = {"name", "backend", "base_url", "api_key", "default_model"}
    items = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not items:
        return
    sets = ", ".join(f"{k}=?" for k in items)
    _exec(f"UPDATE connections SET {sets} WHERE id=?", (*items.values(), conn_id))


def delete_connection(conn_id: int) -> None:
    _exec("DELETE FROM connections WHERE id=?", (conn_id,))


def mark_connection_status(conn_id: int, status: str, version: str = "") -> None:
    _exec(
        "UPDATE connections SET last_status=?, last_version=COALESCE(NULLIF(?,''), last_version),"
        " last_checked=? WHERE id=?",
        (status, version, time.time(), conn_id),
    )


# --------------------------------------------------------------------------
# test_runs
# --------------------------------------------------------------------------
def create_run(conn_id: Optional[int], conn_name: str, model: str, run_type: str,
               prompt_label: str = "", concurrency: int = 1,
               env: Optional[Dict[str, Any]] = None,
               run_config: Optional[Dict[str, Any]] = None,
               concurrency_planned: int = 1) -> int:
    return _exec(
        "INSERT INTO test_runs (conn_id, conn_name, model, run_type, prompt_label, concurrency,"
        " status, started_at, env_json, config_json)"
        " VALUES (?,?,?,?,?,?,'running',datetime('now','localtime'),?,?)",
        (conn_id, conn_name, model, run_type, prompt_label, concurrency,
         json.dumps(env or {}, ensure_ascii=False),
         json.dumps(run_config or {}, ensure_ascii=False)),
    )


def finish_run(run_id: int, summary: Dict[str, Any], status: str = "done") -> None:
    stats = summary.get("stats") or {}
    extra = {k: v for k, v in summary.items() if k != "stats"}
    recommend = summary.get("recommend") or {}
    _exec(
        "UPDATE test_runs SET status=?, finished_at=datetime('now','localtime'), duration_ms=?,"
        " total_reqs=?, ok_reqs=?, avg_tps=?, avg_ttft_ms=?, p50_e2e_ms=?, p90_e2e_ms=?,"
        " p95_e2e_ms=?, p95_ttft_ms=?, p99_e2e_ms=?, avg_e2e_ms=?, prefill_tps=?, decode_tps=?,"
        " throughput_tps=?, rec_concurrency=?, max_qps=?, error_count=?, error_rate=?,"
        " gpu_peak_vram=?, gpu_peak_util=?, gpu_avg_util=?, concurrency=?, summary_json=?"
        " WHERE id=?",
        (
            status,
            summary.get("duration_ms"),
            stats.get("total", 0),
            stats.get("ok", 0),
            stats.get("avg_tps"),
            stats.get("avg_ttft_ms"),
            stats.get("p50_e2e_ms"),
            stats.get("p90_e2e_ms"),
            stats.get("p95_e2e_ms"),
            stats.get("p95_ttft_ms"),
            stats.get("p99_e2e_ms"),
            stats.get("avg_e2e_ms"),
            stats.get("prefill_tps"),
            stats.get("decode_tps"),
            stats.get("throughput_tps"),
            recommend.get("recommended"),
            summary.get("max_qps"),
            stats.get("errors", 0),
            stats.get("error_rate"),
            summary.get("gpu_peak_vram"),
            summary.get("gpu_peak_util"),
            summary.get("gpu_avg_util"),
            summary.get("concurrency") or 1,
            json.dumps(extra, ensure_ascii=False),
            run_id,
        ),
    )


def insert_details(run_id: int, details: Sequence[Dict[str, Any]]) -> None:
    if not details:
        return
    seq = []
    for d in details:
        seq.append((
            run_id,
            d.get("worker_id", 0),
            d.get("case_label"),
            d.get("ttft_ms"),
            d.get("e2e_ms"),
            d.get("prompt_tokens"),
            d.get("completion_tokens"),
            d.get("tps"),
            d.get("prefill_ms"),
            d.get("status"),
            d.get("error"),
        ))
    _exec_many(
        "INSERT INTO request_details (run_id, worker_id, case_label, ttft_ms, e2e_ms,"
        " prompt_tokens, completion_tokens, tps, prefill_ms, status, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        seq,
    )


# --------------------------------------------------------------------------
# eval_items（评测逐题明细）
# --------------------------------------------------------------------------
def insert_eval_item(run_id: int, row: Dict[str, Any]) -> int:
    grade = row.get("grade") or {}
    return _exec(
        "INSERT INTO eval_items (run_id, item_id, case_label, prompt, answer, gold, ok,"
        " metric, detail, grade_json, ttft_ms, e2e_ms, prefill_tps, decode_tps, status, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            row.get("item_id"),
            row.get("case_label"),
            (row.get("prompt") or "")[:2000],
            (row.get("answer") or "")[:4000],
            str(row.get("gold") or "")[:500],
            1 if grade.get("ok") else 0,
            grade.get("metric") or (row.get("meta") or {}).get("metric"),
            str(grade.get("detail") or "")[:500],
            json.dumps(grade, ensure_ascii=False)[:20000],
            row.get("ttft_ms"),
            row.get("e2e_ms"),
            row.get("prefill_tps"),
            row.get("decode_tps"),
            row.get("status"),
            row.get("error"),
        ),
    )


def get_eval_items(run_id: int) -> List[Dict[str, Any]]:
    rows = _rows("SELECT * FROM eval_items WHERE run_id=? ORDER BY id", (run_id,))
    for r in rows:
        try:
            r["grade"] = json.loads(r.pop("grade_json") or "{}")
        except ValueError:
            r["grade"] = {}
    return rows


def list_runs(model: str = "", run_type: str = "", date_from: str = "", date_to: str = "",
              limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    where, args = ["1=1"], []
    if model:
        where.append("model LIKE ?")
        args.append(f"%{model}%")
    if run_type:
        where.append("run_type=?")
        args.append(run_type)
    if date_from:
        where.append("date(started_at) >= date(?)")
        args.append(date_from)
    if date_to:
        where.append("date(started_at) <= date(?)")
        args.append(date_to)
    args += [limit, offset]
    return _rows(
        f"SELECT * FROM test_runs WHERE {' AND '.join(where)}"
        f" ORDER BY id DESC LIMIT ? OFFSET ?",
        args,
    )


def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    return _row("SELECT * FROM test_runs WHERE id=?", (run_id,))


def delete_run(run_id: int) -> None:
    _exec("DELETE FROM test_runs WHERE id=?", (run_id,))


def get_run_details(run_id: int) -> List[Dict[str, Any]]:
    return _rows("SELECT * FROM request_details WHERE run_id=? ORDER BY id", (run_id,))


# --------------------------------------------------------------------------
# metric_samples
# --------------------------------------------------------------------------
SAMPLE_COLUMNS = (
    "ts", "gpu_util", "vram_used_mb", "vram_total_mb", "gpu_temp", "cpu_percent",
    "ram_percent", "ram_used_mb", "proc_rss_mb", "active_reqs", "rps",
    "current_tps", "current_ttft", "gpu_name", "simulated",
)


def insert_sample(s: Dict[str, Any]) -> None:
    _exec(
        f"INSERT OR REPLACE INTO metric_samples ({','.join(SAMPLE_COLUMNS)})"
        f" VALUES ({','.join('?' * len(SAMPLE_COLUMNS))})",
        tuple(s.get(c) for c in SAMPLE_COLUMNS),
    )


def query_samples(seconds: float = 300, max_points: int = 900) -> List[Dict[str, Any]]:
    since = time.time() - float(seconds)
    rows = _rows(
        "SELECT * FROM metric_samples WHERE ts>=? ORDER BY ts ASC", (since,)
    )
    if len(rows) <= max_points:
        return rows
    # 等间隔抽稀，避免前端渲染过多点
    step = len(rows) / float(max_points)
    return [rows[int(i * step)] for i in range(max_points)]


def latest_sample() -> Optional[Dict[str, Any]]:
    return _row("SELECT * FROM metric_samples ORDER BY ts DESC LIMIT 1")


def peak_samples(since: float) -> Dict[str, Any]:
    row = _row(
        "SELECT MAX(vram_used_mb) AS peak_vram, MAX(gpu_util) AS peak_util,"
        " AVG(gpu_util) AS avg_util FROM metric_samples WHERE ts>=?",
        (since,),
    )
    return row or {}


def prune_samples(retention_days: int) -> int:
    cutoff = time.time() - retention_days * 86400
    with _lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM metric_samples WHERE ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


# --------------------------------------------------------------------------
# alerts
# --------------------------------------------------------------------------
def add_alert(rule_key: str, metric: str, value: float, threshold: float, op: str,
              level: str, message: str) -> int:
    return _exec(
        "INSERT INTO alerts (ts, rule_key, metric, value, threshold, op, level, message)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), rule_key, metric, value, threshold, op, level, message),
    )


def list_alerts(limit: int = 100, acked: Optional[int] = None) -> List[Dict[str, Any]]:
    if acked is None:
        return _rows("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))
    return _rows(
        "SELECT * FROM alerts WHERE acked=? ORDER BY id DESC LIMIT ?", (acked, limit)
    )


def ack_alerts(alert_ids: Optional[Sequence[int]] = None) -> None:
    if alert_ids:
        _exec_many("UPDATE alerts SET acked=1 WHERE id=?", [(i,) for i in alert_ids])
    else:
        _exec("UPDATE alerts SET acked=1")


def clear_alerts() -> None:
    _exec("DELETE FROM alerts")


# --------------------------------------------------------------------------
# settings (键值对，存放告警规则 / UI 偏好)
# --------------------------------------------------------------------------
def get_setting(key: str, default: Any = None) -> Any:
    row = _row("SELECT value FROM settings WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    _exec(
        "INSERT INTO settings (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_stats_overview() -> Dict[str, Any]:
    return _row(
        "SELECT (SELECT COUNT(*) FROM test_runs) AS total_runs,"
        " (SELECT COUNT(*) FROM request_details) AS total_requests,"
        " (SELECT MAX(avg_tps) FROM test_runs WHERE avg_tps IS NOT NULL) AS best_tps,"
        " (SELECT COUNT(*) FROM connections) AS total_conns"
    ) or {}


# --------------------------------------------------------------------------
# chat_sessions / chat_messages（对话测试的「记忆」载体）
# --------------------------------------------------------------------------
def list_chat_sessions() -> List[Dict[str, Any]]:
    return _rows(
        "SELECT s.*,"
        " (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id AND m.role='user')"
        "   AS turn_count"
        " FROM chat_sessions s ORDER BY s.updated_at DESC, s.id DESC"
    )


def create_chat_session(title: str, conn_id: Optional[int], model: str,
                        system: str = "", memory: str = "",
                        ctx_budget: Optional[int] = None,
                        keep_turns: Optional[int] = None) -> int:
    return _exec(
        "INSERT INTO chat_sessions (title, conn_id, model, system, memory,"
        " ctx_budget, keep_turns) VALUES (?,?,?,?,?,?,?)",
        (title or "新会话", conn_id, model or "", system or "", memory or "",
         ctx_budget, keep_turns),
    )


def get_chat_session(session_id: int) -> Optional[Dict[str, Any]]:
    return _row("SELECT * FROM chat_sessions WHERE id=?", (session_id,))


def update_chat_session(session_id: int, **fields: Any) -> None:
    allowed = {"title", "conn_id", "model", "system", "memory",
               "ctx_budget", "keep_turns", "total_turns", "total_tokens"}
    items = {k: v for k, v in fields.items() if k in allowed}
    if not items:
        return
    sets = ", ".join(f"{k}=?" for k in items)
    _exec(
        f"UPDATE chat_sessions SET {sets}, updated_at=datetime('now','localtime')"
        f" WHERE id=?",
        (*items.values(), session_id),
    )


def touch_chat_session(session_id: int) -> None:
    _exec("UPDATE chat_sessions SET updated_at=datetime('now','localtime') WHERE id=?",
          (session_id,))


def delete_chat_session(session_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
        conn.commit()


def list_chat_messages(session_id: int, limit: int = 0) -> List[Dict[str, Any]]:
    """按时间正序返回会话消息；``limit>0`` 时只取最近 limit 条（仍按正序返回）。"""
    if limit and limit > 0:
        rows = _rows(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows.reverse()
        return rows
    return _rows("SELECT * FROM chat_messages WHERE session_id=? ORDER BY id",
                 (session_id,))


def add_chat_message(session_id: int, role: str, content: str,
                     run_id: Optional[int] = None, continued: bool = False,
                     truncated: bool = False, finish_reason: str = "",
                     prompt_tokens: Optional[int] = None,
                     completion_tokens: Optional[int] = None,
                     metrics: Optional[Dict[str, Any]] = None) -> int:
    mid = _exec(
        "INSERT INTO chat_messages (session_id, role, content, run_id, continued,"
        " truncated, finish_reason, prompt_tokens, completion_tokens, metrics_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session_id, role, content, run_id, 1 if continued else 0,
         1 if truncated else 0, finish_reason or "", prompt_tokens,
         completion_tokens,
         json.dumps(metrics or {}, ensure_ascii=False, default=str)),
    )
    touch_chat_session(session_id)
    return mid


def delete_chat_messages_from(session_id: int, from_id: int) -> int:
    """删除 id >= from_id 的消息（用于「编辑重发」与「重新生成」）。"""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "DELETE FROM chat_messages WHERE session_id=? AND id>=?",
            (session_id, from_id),
        )
        conn.commit()
        return cur.rowcount


def clear_chat_messages(session_id: int) -> int:
    with _lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        conn.execute(
            "UPDATE chat_sessions SET total_turns=0, total_tokens=0,"
            " updated_at=datetime('now','localtime') WHERE id=?",
            (session_id,),
        )
        conn.commit()
        return cur.rowcount


def delete_chat_message(session_id: int, message_id: int) -> None:
    _exec("DELETE FROM chat_messages WHERE session_id=? AND id=?",
          (session_id, message_id))
    touch_chat_session(session_id)


def refresh_chat_session_stats(session_id: int) -> None:
    """重算会话的轮数与累计 token（由消息表推导，避免计数漂移）。

    轮数 = user 消息条数；token 只累计 assistant 行（提问侧的 token 数
    记在对应的回答上，避免重复计数）。
    """
    row = _row(
        "SELECT"
        " (SELECT COUNT(*) FROM chat_messages WHERE session_id=? AND role='user')"
        "   AS turns,"
        " COALESCE((SELECT SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0))"
        "   FROM chat_messages WHERE session_id=? AND role='assistant'),0) AS toks",
        (session_id, session_id),
    ) or {}
    _exec(
        "UPDATE chat_sessions SET total_turns=?, total_tokens=? WHERE id=?",
        (row.get("turns", 0), int(row.get("toks", 0) or 0), session_id),
    )
