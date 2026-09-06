@echo off
rem ============================================================
rem  FormulaSnip - CMD launcher (start.cmd)
rem
rem  Usage:
rem    start.cmd            launch app (tray background, no window)
rem    start.cmd console    run main.py in foreground (see errors)
rem    start.cmd selftest   run smoke_test.py
rem    start.cmd install    create/refresh .venv deps (rerunnable)
rem
rem  Runtime lookup order (skip re-download, instant start):
rem    1. project-local .venv
rem    2. ready-made FormulaSnip env on this machine
rem       (%USERPROFILE%\.workbuddy\binaries\python\envs\formulasnip)
rem    3. otherwise auto-create .venv with system python / py
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "MODE=%~1"
if not defined MODE set "MODE=start"

rem ---------------- locate a ready interpreter (priority 1/2) ----------------
set "PYW="
set "PY="
if exist ".venv\Scripts\pythonw.exe" (
    set "PYW=%~dp0.venv\Scripts\pythonw.exe"
    set "PY=%~dp0.venv\Scripts\python.exe"
) else if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\formulasnip\Scripts\pythonw.exe" (
    set "PYW=%USERPROFILE%\.workbuddy\binaries\python\envs\formulasnip\Scripts\pythonw.exe"
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\formulasnip\Scripts\python.exe"
)

if "%MODE%"=="install" goto :install
if defined PYW goto :dispatch
goto :bootstrap

rem ---------------- dispatch by sub-command ----------------
:dispatch
if "%MODE%"=="selftest" goto :selftest
if "%MODE%"=="console"  goto :console
goto :start

:start
start "" "%PYW%" main.py
echo.
echo  [OK] FormulaSnip started - no main window on purpose, same as Snipaste.
echo      It runs in the SYSTEM TRAY. Press F1 anywhere to select a region.
echo      - Tray icon not visible?  click the ^ arrow next to the clock.
echo      - First launch: a balloon tip and the Settings window appear.
echo      - Nothing appeared?  run:  start.cmd console   to see the error,
echo        or open the log file:  %USERPROFILE%\.formulasnip\app.log
exit /b 0

:console
echo [RUN] running main.py in foreground (Ctrl+C to quit) ...
"%PY%" main.py
set "RC=%errorlevel%"
echo [EXIT] exit code = %RC%
pause
exit /b %RC%

:selftest
echo [TEST] running smoke_test.py ...
"%PY%" -u smoke_test.py
set "RC=%errorlevel%"
echo [DONE] selftest exit code = %RC%   (0 = all passed)
pause
exit /b %RC%

rem ---------------- no ready env: auto create .venv ----------------
:bootstrap
echo [SETUP] no ready env found, preparing .venv with system Python ...
goto :find_py

:install
echo [SETUP] install/update dependencies (install mode) ...
if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] .venv not found, creating it ...
    goto :find_py
)
goto :pip_step

:find_py
set "PYBASE="
where python >nul 2>nul && set "PYBASE=python"
if not defined PYBASE (
    where py >nul 2>nul && set "PYBASE=py -3"
)
if not defined PYBASE (
    echo [ERROR] Python not found. Please install Python 3.10+ first:
    echo        https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [SETUP] using interpreter: %PYBASE%
if not exist ".venv\Scripts\python.exe" (
    %PYBASE% -m venv ".venv"
    if errorlevel 1 goto :setup_fail
)
goto :pip_step

:pip_step
echo [SETUP] installing dependencies (PySide6 is large, please wait) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if not errorlevel 1 goto :pip_ok
echo [SETUP] default index failed, retry with TUNA mirror ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 goto :setup_fail

:pip_ok
if "%MODE%"=="install" (
    echo.
    echo  [OK] dependencies ready. Run start.cmd to launch; start.cmd selftest to verify.
    pause
    exit /b 0
)
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "PY=%~dp0.venv\Scripts\python.exe"
echo [SETUP] environment ready.
goto :dispatch

:setup_fail
echo [ERROR] setup failed, check messages above / network and retry.
pause
exit /b 1
