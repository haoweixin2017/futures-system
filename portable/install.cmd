@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; & '%~dp0install.ps1'"
if errorlevel 1 pause
