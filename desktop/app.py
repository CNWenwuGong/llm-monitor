#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地大模型实时性能监控测试平台 · 桌面端启动器

把一个 FastAPI 后端和一块原生窗口合进**同一个进程**：

    主线程   → pywebview 窗口（GUI 必须在主线程；Windows 上跑的是 WebView2）
    后台线程 → uvicorn，绑定 127.0.0.1 上的一个空闲端口

为什么不直接把地址丢给系统浏览器：桌面版要的是「双击即用」——独立窗口、
没有地址栏、关掉窗口服务跟着退、数据落在用户目录而不是程序目录。

开发模式直接跑::

    .venv/Scripts/python.exe desktop/app.py

打包后的 exe 由 ``desktop/llm_monitor.spec`` 产出。

命令行开关（也用于自动化验证）::

    --no-window        只起服务不开窗口，把运行时信息写进 JSON 后前台驻留
    --port 8899        指定端口
    --attach 8081      直接复用已在跑的实例，不再启动新服务
    --debug            打开窗口调试器
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

APP_TITLE = "本地大模型实时性能监控测试平台"
APP_ID = "llm-monitor"
# 与 backend/main.py 里 FastAPI(version=...) 保持一致，用来识别
# 「这个端口上跑的到底是不是我们自己」，避免误 attach 到别人的服务。
APP_VERSION = "1.0.0"
DEFAULT_PORTS = (8081, 8080)
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"

_LOG_STREAM = None  # 持有日志文件句柄，防止被 GC 提前关闭


# --------------------------------------------------------------------------
# 路径：只读资源 vs 可写数据
# --------------------------------------------------------------------------
def resource_root() -> Path:
    """只读资源根目录（frontend/ 与 backend/prompts/ 的父目录）。

    onefile 打包后 PyInstaller 会把数据解压到 ``sys._MEIPASS``，而
    ``backend/config.py`` 的 ``BASE_DIR = Path(__file__).parent.parent`` 恰好
    就落在那里——所以**不需要**为了打包去改后端代码，只要保证
    ``frontend/`` 与 ``backend/prompts/`` 被收进包里即可。
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", str(Path(sys.executable).parent)))
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """可写数据目录。

    绝不能落在 ``_MEIPASS``：那是每次启动重建的临时目录，数据库跟着一起消失，
    表现为「每次打开历史记录都空了」。所以桌面版一律把库放到用户目录。
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / APP_ID


def log(msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    try:
        print(f"{stamp} [desktop] {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - 日志本身不允许拖垮启动
        pass


def _redirect_dead_streams(log_dir: Path) -> None:
    """``--noconsole`` 打包后 sys.stdout / sys.stderr 是 None。

    uvicorn 的日志 handler 会在第一次写的时候抛 ``'NoneType' has no attribute
    'write'``，把整个服务带崩。所以在 **import backend 之前**先把它们接到
    日志文件上——顺带让桌面版的日志可追溯。
    """
    global _LOG_STREAM
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    _LOG_STREAM = open(log_dir / "desktop.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _LOG_STREAM
    if sys.stderr is None:
        sys.stderr = _LOG_STREAM


def prepare_runtime() -> dict:
    """把环境变量、sys.path、日志流全部备好。

    ⚠️ 顺序有硬要求：``backend/config.py`` 在 **import 的那一刻**就把
    ``DB_PATH`` 求值了，所以 ``LLM_MONITOR_DB`` 必须在这之前设好，
    否则改不动它。
    """
    data_dir = user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _redirect_dead_streams(data_dir / "logs")

    root = resource_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    os.environ["PYTHONUTF8"] = "1"
    os.environ.setdefault("LLM_MONITOR_HOST", "127.0.0.1")  # 桌面版只监听本机
    os.environ.setdefault("LLM_MONITOR_DB", str(data_dir / "monitor.db"))
    return {"root": root, "data": data_dir, "db": Path(os.environ["LLM_MONITOR_DB"])}


# --------------------------------------------------------------------------
# 端口探测
# --------------------------------------------------------------------------
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开系统代理


def probe_instance(port: int, timeout: float = 1.2) -> dict | None:
    """端口上如果跑着我们的服务，返回它的 /api/health，否则 None。"""
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 连不上就是没有
        return None
    if isinstance(info, dict) and info.get("ok") and info.get("version") == APP_VERSION:
        return info
    return None


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_port() -> int:
    """让内核挑一个空闲端口。

    本机 8080 常年被用户的 llama.cpp server 占着，8081 也可能被手动起的
    实例占着——所以端口不能写死，绑定 0 让系统分配最稳妥。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --------------------------------------------------------------------------
# 后端服务线程
# --------------------------------------------------------------------------
class ServerThread(threading.Thread):
    """在后台线程里跑 uvicorn。

    GUI 必须待在主线程，所以服务只能放后台——uvicorn 自己会检测到
    「非主线程」并跳过信号注册，不会有冲突。
    """

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True, name="llm-monitor-server")
        self.port = port
        self.server = None
        self.error: BaseException | None = None

    def run(self) -> None:  # noqa: D102
        try:
            import uvicorn

            from backend.main import app

            # 显式点名 http/ws/loop 实现，避免 uvicorn 的 "auto" 在打包后
            # 动态导入失败（那串模块路径是运行时拼出来的，PyInstaller 的静态
            # 分析看不见）。三者都挑了纯 Python 实现：
            #   h11              —— 不用 httptools（C 扩展，要额外收二进制）
            #   websockets-sansio —— uvicorn 推荐值，老的 "websockets" 会打弃用警告
            #   asyncio          —— Windows 上本来也没有 uvloop
            cfg = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                http="h11",
                ws="websockets-sansio",
                loop="asyncio",
                log_level="info",
                access_log=False,
                log_config=None,  # 用 backend.main 里已经配好的 logging
            )
            self.server = uvicorn.Server(cfg)
            # 双保险：非主线程注册信号本来就该跳过
            self.server.install_signal_handlers = lambda: None
            self.server.run()
        except BaseException as exc:  # noqa: BLE001 - 要把异常带回主线程
            self.error = exc
            log(f"服务异常退出: {exc!r}")

    def wait_ready(self, timeout: float = 90.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.error is not None:
                return False
            if probe_instance(self.port, 0.6):
                return True
            time.sleep(0.2)
        return False

    def shutdown(self, timeout: float = 10.0) -> None:
        """让 uvicorn 走完 lifespan 的正常关闭流程，再等它收尾。"""
        if self.server is not None:
            self.server.should_exit = True
        self.join(timeout=timeout)
        if self.is_alive():
            log("服务未在超时内退出（数据库仍在写入？）")


# --------------------------------------------------------------------------
# WebView2 运行时
# --------------------------------------------------------------------------
def webview2_available() -> bool:
    """Windows 上 WebView2 缺失会让 pywebview 抛栈，不如提前给个明白话。

    Win11 与较新的 Win10 都预装了；只有精简版系统 / 老旧 Win10 需要手动装。
    """
    if sys.platform != "win32":
        return True

    import winreg

    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"  # WebView2 Evergreen Runtime
    for hive, sub in (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
            if version and version != "0.0.0.0":
                return True
        except OSError:
            continue

    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base and (Path(base) / "Microsoft" / "EdgeWebView" / "Application").is_dir():
            return True
    return False


def message_box(text: str, title: str = APP_TITLE) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)  # MB_ICONINFORMATION
            return
        except Exception:  # noqa: BLE001
            pass
    print(f"[{title}] {text}", file=sys.stderr)


def open_window(url: str, data_dir: Path, debug: bool) -> None:
    """开窗并阻塞到用户关窗为止。"""
    import webview

    webview.create_window(
        APP_TITLE,
        url,
        width=1480,
        height=940,
        min_size=(1080, 680),
        background_color="#0e1117",
        text_select=True,
        confirm_close=False,
    )
    webview.start(
        gui="edgechromium",
        debug=debug,
        # 前端把「上次打开的会话 / 回答长度开关」记在 localStorage 里，
        # private_mode 默认是 True，不改的话这些偏好每次启动都会被清空。
        private_mode=False,
        storage_path=str(data_dir / "webview"),
    )


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=APP_TITLE, add_help=True)
    p.add_argument("--port", type=int, default=None, help="指定监听端口（默认自动选择）")
    p.add_argument("--attach", type=int, default=None, metavar="PORT", help="复用已在运行的实例")
    p.add_argument("--no-window", action="store_true", help="只起服务不开窗口")
    p.add_argument("--debug", action="store_true", help="打开窗口调试器")
    return p.parse_args(argv)


def write_runtime(data_dir: Path, payload: dict) -> None:
    """把本次运行的端口/库路径落一份，方便排查，也供外部脚本读取。"""
    try:
        (data_dir / "runtime.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log(f"写 runtime.json 失败: {exc}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = prepare_runtime()
    data_dir, root, db_path = env["data"], env["root"], env["db"]
    log(f"资源目录 {root}")
    log(f"数据目录 {db_path}")

    # ---- 决定端口：优先复用已在跑的实例，其次默认端口，最后交给内核 ----
    reused: int | None = None
    if args.attach:
        if not probe_instance(args.attach):
            message_box(f"指定的实例不可达：127.0.0.1:{args.attach}")
            return 4
        reused = args.attach
    else:
        candidates = [args.port] if args.port else list(DEFAULT_PORTS)
        for candidate in candidates:
            if candidate and probe_instance(candidate):
                reused = candidate
                log(f"端口 {candidate} 上已有本程序在跑，直接复用其数据与服务")
                break

    server: ServerThread | None = None
    if reused:
        port = reused
    else:
        port = args.port or next((p for p in DEFAULT_PORTS if port_available(p)), free_port())

    # backend/config.py 在 **import 的那一刻**就读走了 LLM_MONITOR_PORT，
    # 而那个 import 发生在服务线程里——所以这里必须先设好，否则启动日志会
    # 打印默认的 8080，跟真实监听端口对不上，排查时极其误导。
    os.environ["LLM_MONITOR_PORT"] = str(port)

    if not reused:
        server = ServerThread(port)
        server.start()
        if not server.wait_ready():
            detail = repr(server.error) if server.error else "启动超时"
            log(f"后端启动失败: {detail}")
            if not args.no_window:
                message_box(f"后端启动失败：{detail}\n\n日志：{data_dir / 'logs' / 'desktop.log'}")
            return 5
        log(f"后端就绪 → http://127.0.0.1:{port}")

    url = f"http://127.0.0.1:{port}/"
    write_runtime(data_dir, {
        "pid": os.getpid(), "port": port, "url": url,
        "db": str(db_path), "reused_existing": bool(reused),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    try:
        if args.no_window:
            print(json.dumps({
                "ok": True, "port": port, "url": url,
                "db": str(db_path), "reused": bool(reused),
            }, ensure_ascii=False), flush=True)
            log("已进入 --no-window 模式，Ctrl+C 结束")
            while True:
                time.sleep(1)
        else:
            if not webview2_available():
                message_box(
                    "未检测到 Microsoft Edge WebView2 运行时，窗口无法显示。\n\n"
                    f"请安装后重试：{WEBVIEW2_URL}\n\n"
                    "（Win11 与较新的 Win10 已自带）"
                )
                return 3
            log("打开窗口…")
            open_window(url, data_dir, args.debug)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            log("窗口已关闭，正在停止后端…")
            server.shutdown()
        log("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
