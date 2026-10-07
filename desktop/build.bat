@echo off
REM ===== 本地大模型实时性能监控测试平台 · 桌面端一键打包 (Windows) =====
REM
REM   desktop\build.bat             打成单文件 dist\LLM-Monitor.exe（默认）
REM   desktop\build.bat onedir      打成目录版 dist\LLM-Monitor\（启动快，适合内网分发）
REM
REM 产物双击即可运行；数据写在 %APPDATA%\llm-monitor，不碰程序目录。

chcp 65001 > nul
setlocal
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "LM_ONEFILE=1"
set "FORM=单文件 exe"
for %%A in (%*) do (
  if /i "%%~A"=="onedir"  set "LM_ONEFILE=0"
  if /i "%%~A"=="onefile" set "LM_ONEFILE=1"
)
if "%LM_ONEFILE%"=="0" set "FORM=目录版"

echo ==^> 检查打包依赖
"%PY%" -c "import webview, PyInstaller" 2>nul
if errorlevel 1 (
  echo ==^> 缺少依赖，正在安装
  "%PY%" -m pip install -r desktop\requirements-desktop.txt || goto :err
)

echo ==^> 生成图标
"%PY%" desktop\make_icon.py || goto :err

echo ==^> 开始打包（%FORM%）
"%PY%" -m PyInstaller desktop\llm_monitor.spec --noconfirm || goto :err

echo.
echo ============================================================
if "%LM_ONEFILE%"=="0" (
  echo  产物: dist\LLM-Monitor\LLM-Monitor.exe
) else (
  echo  产物: dist\LLM-Monitor.exe
)
echo  运行数据: %%APPDATA%%\llm-monitor
echo ============================================================
goto :eof

:err
echo.
echo 打包失败，请查看上面的输出。
exit /b 1
