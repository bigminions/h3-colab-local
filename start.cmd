@echo off
cd /d "%~dp0"
set "H3PY=%~dp0.venv\Scripts\python.exe"
if not exist "%H3PY%" set "H3PY=%~dp0..\.venv-h3\Scripts\python.exe"
if not exist "%H3PY%" (
    echo Python environment is missing. Create .venv and install requirements.txt. See README.md.
    pause
    exit /b 1
)
"%H3PY%" launch.py
if errorlevel 1 pause
