@echo off
rem ============================================================
rem  FormulaSnip launcher: create venv if needed, then start GUI
rem ============================================================
setlocal
cd /d "%~dp0"

set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
    where py >nul 2>nul && set "PYEXE=py -3"
)
if not defined PYEXE (
    echo [ERROR] Python 3.10+ not found. Install from https://www.python.org/
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    %PYEXE% -m venv .venv
    if errorlevel 1 goto :err
    echo [setup] Installing dependencies, this may take a while...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :err
)

echo [run] Starting FormulaSnip (tray icon, press F1 to capture)...
start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0

:err
echo [ERROR] Setup failed. Please check the messages above.
pause
exit /b 1
