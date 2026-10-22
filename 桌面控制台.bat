@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo 未找到 .venv\Scripts\pythonw.exe
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "desktop_console.py"
