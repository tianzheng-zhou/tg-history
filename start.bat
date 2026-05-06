@echo off
chcp 65001 >nul
title TG-History 一键启动
setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo ============================================
echo   Telegram 群聊智能分析系统 - 一键启动
echo   前端: http://localhost:13747
echo   后端: http://localhost:13748
echo ============================================
echo.

if not exist "%ROOT%\backend\main.py" (
    echo [ERROR] 未找到 backend\main.py，当前 ROOT=%ROOT%
    pause
    exit /b 1
)

if not exist "%ROOT%\frontend\package.json" (
    echo [ERROR] 未找到 frontend\package.json，当前 ROOT=%ROOT%
    pause
    exit /b 1
)

echo [启动] 后端 uvicorn :13748 ...
start "TG-History Backend" /D "%ROOT%" cmd /K "title TG-History Backend && if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat && uvicorn backend.main:app --host 0.0.0.0 --port 13748 --reload"

timeout /t 2 /nobreak >nul

echo [启动] 前端 vite :13747 ...
start "TG-History Frontend" /D "%ROOT%\frontend" cmd /K "title TG-History Frontend && npm run dev"

echo.
echo [OK] 已发起启动，请查看 Backend / Frontend 两个窗口。
echo      前端地址: http://localhost:13747
echo.
pause
