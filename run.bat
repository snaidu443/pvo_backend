@echo off
rem Start the PVO API locally. Works from PowerShell or Command Prompt:  .\run.bat
rem (Activates the venv inside this script, so the PowerShell script policy does not matter.)
cd /d "%~dp0"
if not exist venv\Scripts\activate.bat (
  echo No venv found. Create it first:  py -m venv venv
  exit /b 1
)
call venv\Scripts\activate.bat
python -m uvicorn app.main:app --reload --port 8000
