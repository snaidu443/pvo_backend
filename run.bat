@echo off
rem Start the PVO API locally. Works from PowerShell or Command Prompt:  .\run.bat
rem (Activates the venv inside this script, so the PowerShell script policy does not matter.)
rem
rem Default: NO auto-reload. A reload restarts the server whenever a .py file changes, which cuts running requests
rem (ECONNRESET), stops a running AI extraction and signs everyone out (the local sign-in key lives in memory).
rem While you are changing backend code use:  .\run.bat reload
cd /d "%~dp0"
if not exist venv\Scripts\activate.bat (
  echo No venv found. Create it first:  py -m venv venv
  exit /b 1
)
call venv\Scripts\activate.bat
if /i "%~1"=="reload" (
  python -m uvicorn app.main:app --reload --reload-dir app --port 8000
) else (
  python -m uvicorn app.main:app --port 8000
)
