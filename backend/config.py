"""全局配置：路径、采样频率、熔断阈值等。"""

from __future__ import annotations

import os
from pathlib import Path

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = BASE_DIR / "backend"
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

DB_PATH = Path(os.environ.get("LLM_MONITOR_DB", DATA_DIR / "monitor.db"))

# ---------- 服务 ----------
HOST = os.environ.get("LLM_MONITOR_HOST", "0.0.0.0")
PORT = int(os.environ.get("LLM_MONITOR_PORT", "8080"))

# ---------- 采集 ----------
SAMPLE_INTERVAL = float(os.environ.get("LLM_MONITOR_SAMPLE_INTERVAL", "2"))  # 秒
SAMPLE_RETENTION_DAYS = int(os.environ.get("LLM_MONITOR_RETENTION_DAYS", "7"))
RPS_WINDOW = 10.0  # 计算 RPS 的滑动窗口（秒）
TPS_EMA_ALPHA = 0.35  # 当前 TPS 的指数平滑系数

# ---------- 连接探测 ----------
HEALTH_INTERVAL = 10.0  # 心跳探测间隔（秒）
CONNECT_TIMEOUT = 8.0
DEFAULT_REQUEST_TIMEOUT = 300.0

# ---------- 对话测试（多轮 + 记忆） ----------
# 上下文预算：一次请求最多允许携带多少 token 的历史。
# 真实后端的上下文上限由模型自身决定，这里只做"发送侧"的保守裁剪，
# 避免把整段长对话塞爆模型上下文。前端可在参数面板覆盖。
CHAT_CTX_BUDGET = int(os.environ.get("LLM_MONITOR_CHAT_CTX_BUDGET", "8192"))
# 无论如何至少保留最近多少轮对话（1 轮 = user + assistant），
# 即使已经超出预算也不再继续向前裁剪，避免"聊着聊着把刚才的话忘了"。
CHAT_KEEP_TURNS = int(os.environ.get("LLM_MONITOR_CHAT_KEEP_TURNS", "6"))
# 单条消息超过该字符数时按比例压缩（长文档粘贴场景），防止一条消息吃满预算
CHAT_MAX_MSG_CHARS = int(os.environ.get("LLM_MONITOR_CHAT_MAX_MSG_CHARS", "24000"))
# 生成回答时默认预留多少 token 给输出
CHAT_DEFAULT_MAX_TOKENS = int(os.environ.get("LLM_MONITOR_CHAT_MAX_TOKENS", "1024"))

# ---------- 压测保护（熔断） ----------
BREAKER_ERROR_RATE = 0.20  # 错误率高于 20% 自动停止
BREAKER_VRAM_PCT = 95.0  # 显存占用高于 95% 自动停止
BREAKER_CONSECUTIVE_ERRORS = 20  # 连续错误数保护

DEFAULT_GPU_TOTAL_MB = 8192.0  # 无 GPU 且开启模拟时的虚拟显存总量
