@echo off
cd /d "%~dp0"
uvicorn main:app --reload --host 127.0.0.1 --port 8000
pause