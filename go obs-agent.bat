@echo off
chcp 65001 >nul
title OBS-Agent
cd /d "%~dp0"
".venv\Scripts\python.exe" agent.py
pause
