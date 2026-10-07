#!/usr/bin/env bash
# ===== 本地大模型实时性能监控测试平台 · 桌面端一键打包 (macOS / Linux) =====
#
#   bash desktop/build.sh           打成单文件 dist/LLM-Monitor
#   bash desktop/build.sh onedir    打成目录版 dist/LLM-Monitor/
#
# 注：macOS / Linux 上 pywebview 需要系统 WebView 组件
#     （macOS 自带 WKWebView；Linux 需 python3-gi + WebKit2GTK）。

set -e
cd "$(dirname "$0")/.."

PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY=".venv/bin/python"
[ -x "$PY" ] || PY="${PYTHON:-python3}"

LM_ONEFILE=1
FORM="单文件"
for arg in "$@"; do
  case "$arg" in
    onedir)  LM_ONEFILE=0 ;;
    onefile) LM_ONEFILE=1 ;;
  esac
done
[ "$LM_ONEFILE" = "0" ] && FORM="目录版"
export LM_ONEFILE

echo "==> 检查打包依赖"
"$PY" -c "import webview, PyInstaller" 2>/dev/null || {
  echo "==> 缺少依赖，正在安装"
  "$PY" -m pip install -r desktop/requirements-desktop.txt
}

echo "==> 生成图标"
"$PY" desktop/make_icon.py

echo "==> 开始打包（${FORM}）"
"$PY" -m PyInstaller desktop/llm_monitor.spec --noconfirm

echo
echo "============================================================"
if [ "$LM_ONEFILE" = "0" ]; then
  echo " 产物: dist/LLM-Monitor/LLM-Monitor"
else
  echo " 产物: dist/LLM-Monitor"
fi
echo " 运行数据: ~/.local/share/llm-monitor（macOS 为 ~/Library/Application Support/llm-monitor）"
echo "============================================================"
