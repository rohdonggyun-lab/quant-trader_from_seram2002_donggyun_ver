@echo off
rem 가상 매매 실행 (작업 스케줄러 등록용)
cd /d "%~dp0"
"C:\workspaces\quant\quant.venv\Scripts\python.exe" run_paper.py
