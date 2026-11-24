@echo off
rem quant-trader run (for Task Scheduler)
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=..\.venv\Scripts\python.exe
"%PY%" run.py
