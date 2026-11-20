@echo off
REM Launches the SEC Form 4 Insider Scanner without a console window.
setlocal
cd /d "%~dp0"

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "%~dp0form4_scanner.py"
    exit /b 0
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0form4_scanner.py"
    exit /b 0
)

echo.
echo  Python 3 was not found on this computer.
echo.
echo  Install it (free) using ONE of these options, then double-click this file again:
echo    1) Open PowerShell and run:   winget install -e --id Python.Python.3.12
echo    2) Download from https://www.python.org/downloads/windows/
echo       (tick "Add python.exe to PATH" during setup)
echo.
pause
