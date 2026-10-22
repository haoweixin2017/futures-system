@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "FUTURES_PROFILE=local"
cd /d "%~dp0"

echo =======================================================
echo          国内期货实时分析与交易系统 停止中...
echo =======================================================

rem ---- 通过监听端口定位所有相关进程并结束 ----
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"[.:]8000 .*LISTENING"') do (
    if not "%%p"=="0" (
        echo 结束进程 PID=%%p
        taskkill /PID %%p /T /F >nul 2>&1
        set FOUND=1
    )
)

if "%FOUND%"=="0" (
    echo 端口 8000 未被占用，服务未在运行。
) else (
    echo 服务已停止。
)

timeout /t 2 /nobreak >nul
endlocal
