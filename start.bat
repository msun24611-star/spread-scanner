@echo off
REM ===========================================================
REM  start.bat -- launcher for spread-scanner
REM  Keep this file pure ASCII: cmd.exe parses .bat with the
REM  legacy code page, and non-ASCII text corrupts the parser.
REM  All checks and Chinese messages live in start.py.
REM ===========================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo   [X] venv not found: %PY%
    echo       python -m venv .venv
    echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

"%PY%" "%~dp0start.py"
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
