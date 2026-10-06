set PYTHONUTF8=1
@echo off

REM ===== 本地大模型实时性能监控测试平台 · 一键启动 (Windows) =====
setlocal
cd /d "%~dp0"

if "%PYTHON%"=="" set PYTHON=python
if "%LLM_MONITOR_HOST%"=="" set LLM_MONITOR_HOST=0.0.0.0
if "%LLM_MONITOR_PORT%"=="" set LLM_MONITOR_PORT=8081

echo ==^> 检查依赖
%PYTHON% -c "import fastapi, uvicorn, httpx, psutil" 2>nul
if errorlevel 1 (
  echo ==^> 安装依赖
  %PYTHON% -m pip install -r requirements.txt || goto :err
)

if not exist data mkdir data
if not exist logs mkdir logs

echo ==^> 启动服务 http://localhost:%LLM_MONITOR_PORT%
%PYTHON% -m uvicorn backend.main:app --host %LLM_MONITOR_HOST% --port %LLM_MONITOR_PORT% --log-level info
goto :eof

:err
echo 依赖安装失败，请手动执行: %PYTHON% -m pip install -r requirements.txt
pause
