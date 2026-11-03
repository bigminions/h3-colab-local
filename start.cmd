@echo off
cd /d "%~dp0"
set "LSTUDIO_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%LSTUDIO_PY%" (
    echo Python environment is missing. Create .venv and install requirements.txt. See README.md.
    pause
    exit /b 1
)
"%LSTUDIO_PY%" launch.py
if errorlevel 1 pause
