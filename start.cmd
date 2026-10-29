@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ClaudePipeline
python server\main.py %*
if errorlevel 1 pause
