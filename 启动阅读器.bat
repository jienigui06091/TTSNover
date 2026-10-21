@echo off
chcp 65001 >nul
title TTSNover 网页版阅读器
cd /d "%~dp0"

echo ========================================================
echo         TTSNover 极简美学阅读器服务正在启动...
echo ========================================================
echo.
echo 服务地址: http://localhost:8848/
echo 按 Ctrl+C 可停止服务
echo.

start http://localhost:8848/
python server.py

pause
