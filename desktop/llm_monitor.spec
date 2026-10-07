# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 llm-monitor 打成双击即用的桌面程序。

用法（一般不用手敲，走 desktop/build.bat）::

    pyinstaller desktop/llm_monitor.spec --noconfirm

产物形态由环境变量 ``LM_ONEFILE`` 决定：

    LM_ONEFILE=1（默认）  单文件 dist/LLM-Monitor.exe，好拷贝，启动要解压 3-5s
    LM_ONEFILE=0          目录版 dist/LLM-Monitor/，启动 <1s，内网分发可直接压 zip

几个必须踩准的点：

1. **静态前端与提示词必须显式收进来**。``backend/config.py`` 用
   ``BASE_DIR = Path(__file__).parent.parent`` 定位 ``frontend/``，
   打包后这个 BASE_DIR 正好等于 ``sys._MEIPASS``，所以只要把目录放到
   对应的相对位置，后端代码一行都不用改。
2. **uvicorn 的实现类是按字符串拼模块名加载的**，静态分析看不见，
   必须写进 hiddenimports，否则运行时报 "Could not import module"。
3. **数据库不能落在 _MEIPASS**（那是每次启动重建的临时目录），
   这件事由 ``desktop/app.py`` 在设置 ``LLM_MONITOR_DB`` 时解决。
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821 - SPECPATH 由 PyInstaller 注入
ONEFILE = os.environ.get("LM_ONEFILE", "1") != "0"
NAME = "LLM-Monitor"

# --------------------------------------------------------------------------
# 随包分发的只读资源
# --------------------------------------------------------------------------
datas = [
    (str(ROOT / "frontend"), "frontend"),                     # 含 vendor/echarts.min.js
    (str(ROOT / "backend" / "prompts"), "backend/prompts"),   # 基准用例与评测套件定义
]

# --------------------------------------------------------------------------
# 动态导入：静态分析找不到，只能点名
# --------------------------------------------------------------------------
hiddenimports = [
    # uvicorn 按字符串拼接实现类的模块路径
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_sansio_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 后端在 try/except 里 import 的（拿不到就降级，所以容易被漏收）
    "pynvml",
    "psutil",
    "httpx",
    "httpcore",
    "h11",
    "websockets",
    "websockets.legacy",
    "anyio",
    "anyio._backends._asyncio",
    # 桌面壳：Windows 走 WebView2（其余平台后端不收，省体积）
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]

# --------------------------------------------------------------------------
# 排除用不上的大件
# --------------------------------------------------------------------------
excludes = [
    # pywebview 的可选 GUI 后端，Windows 上只用 WebView2
    "cefpython3", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy",
    # 数据分析栈，本项目后端不依赖
    "numpy", "pandas", "scipy", "matplotlib", "PIL",
    # 开发期工具
    "pytest", "IPython", "jupyter", "notebook",
    "pyinstaller", "PyInstaller",
]

block_cipher = None

a = Analysis(                                                    # noqa: F821
    [str(ROOT / "desktop" / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)            # noqa: F821

_exe_kwargs = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX 压缩换来的是启动变慢 + 杀软误报，不值
    console=False,                   # 桌面程序，不要黑框
    disable_windowed_traceback=False,  # 崩溃时弹窗给 traceback，别静默退出
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "desktop" / "assets" / "app.ico"),
    version=str(ROOT / "desktop" / "version_info.txt"),
)

if ONEFILE:
    exe = EXE(                                                   # noqa: F821
        pyz, a.scripts, a.binaries, a.datas, [],
        runtime_tmpdir=None,
        **_exe_kwargs,
    )
else:
    exe = EXE(                                                   # noqa: F821
        pyz, a.scripts, [],
        exclude_binaries=True,
        **_exe_kwargs,
    )
    coll = COLLECT(                                              # noqa: F821
        exe, a.binaries, a.datas,
        strip=False, upx=False, name=NAME,
    )
