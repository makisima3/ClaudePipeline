@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AgentPipeline - codex
where python >nul 2>nul
if errorlevel 1 (
  echo Нужен Python 3.10+ с командой python: https://www.python.org/downloads/
  pause
  exit /b 1
)
python server\main.py --agent codex %*
if errorlevel 1 pause
