#!/usr/bin/env bash
# ===== 本地大模型实时性能监控测试平台 · 一键启动 =====
# 用法：bash run.sh  （Windows 可用 Git Bash / WSL 运行）
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

HOST="${LLM_MONITOR_HOST:-0.0.0.0}"
PORT="${LLM_MONITOR_PORT:-8080}"

echo "==> 检查依赖"
"$PY" -c "import fastapi, uvicorn, httpx, psutil" 2>/dev/null || {
  echo "==> 安装依赖到当前 Python 环境"
  "$PY" -m pip install -r requirements.txt
}

echo "==> 创建数据目录"
mkdir -p data logs

echo "==> 启动服务 http://localhost:${PORT}"
exec "$PY" -m uvicorn backend.main:app --host "$HOST" --port "$PORT" --log-level info
