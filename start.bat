@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "FUTURES_PROFILE=local"
cd /d "%~dp0"

echo =======================================================
echo       国内期货模拟交易系统（新浪行情）启动中...
echo =======================================================

rem ---- 检查虚拟环境 ----
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境: .venv\Scripts\python.exe
    echo        请先创建虚拟环境并安装依赖:
    echo            python -m venv .venv
    echo            .venv\Scripts\pip.exe install -r requirements.txt
    pause
    exit /b 1
)

rem ---- 检查端口占用 ----
netstat -ano | findstr /r /c:"[.:]8000 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [提示] 端口 8000 已被占用，服务可能已在运行。
    echo        如需重启，请先执行 stop.bat，或手动结束占用进程。
    pause
    exit /b 0
)

echo 正在启动核心服务 (仅模拟用户, 新浪行情, 端口: 8000)...
rem Launch through run.py to avoid command-line quoting issues.
start "Citic Futures Server" cmd /k ".venv\Scripts\python.exe run.py"

echo 等待服务就绪...
timeout /t 4 /nobreak >nul

echo =======================================================
echo 已发起启动，请查看服务窗口。
echo MySQL/Redis: auto-start if needed; please allow Windows UAC.
echo Web 监控看板: http://127.0.0.1:8000
echo API 文档地址: http://127.0.0.1:8000/docs
echo =======================================================

pause
endlocal
