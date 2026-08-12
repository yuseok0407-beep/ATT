@echo off
setlocal
set "PROJECT_DIR=%~dp0"

start "auto2 dashboard" "%PROJECT_DIR%venv\Scripts\python.exe" "%PROJECT_DIR%dashboard\app.py"

timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:5055"
