@echo off
chcp 65001 >nul
title QuantDesktop
cd /d "%~dp0\dist\QuantDesktop"
rem 清理占用8000的残留进程（防白屏）
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1
start "" "QuantDesktop.exe"
echo.
echo 已启动，窗口约3-10秒弹出。
echo 若10秒后仍无窗口：看 dist\QuantDesktop\startup.log，SmartScreen 弹窗时点「更多信息」-「仍要运行」。
timeout /t 8 >nul
