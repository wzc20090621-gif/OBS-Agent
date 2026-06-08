@echo off
chcp 65001 >nul
title OBS-Agent

cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ❌ 未找到 Python，请先安装 Python 并添加到 PATH
    pause
    exit /b 1
)

echo 🚀 正在启动智能体...
echo.

python agent.py
pause
