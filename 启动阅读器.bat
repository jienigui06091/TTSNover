@echo off
chcp 65001 >nul
title TTSNover 多用户网页版阅读器
cd /d "%~dp0"

set PORT=8848
echo ========================================================
echo       TTSNover 多用户极简美学阅读器正在启动...
echo ========================================================
echo.
echo 服务地址: http://localhost:%PORT%/
echo 按 Ctrl+C 可停止服务
echo.

start http://localhost:%PORT%/
python -m uvicorn app:app --host 0.0.0.0 --port %PORT%

pause
