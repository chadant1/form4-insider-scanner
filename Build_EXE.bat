@echo off
REM Builds a single-file Form4Scanner.exe (no Python needed to run it afterwards).
setlocal
cd /d "%~dp0"

set "PY=py"
where py >nul 2>nul || set "PY=python"
%PY% --version >nul 2>nul || (
    echo Python 3 is required to BUILD the exe. Install it with:  winget install -e --id Python.Python.3.12
    pause
    exit /b 1
)

echo Installing PyInstaller...
%PY% -m pip install --upgrade pyinstaller || goto :fail

echo Building Form4Scanner.exe ...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name Form4Scanner --icon assets\app.ico --add-data "assets\app.ico;assets" --add-data "assets\app.png;assets" form4_scanner.py || goto :fail

copy /y "dist\Form4Scanner.exe" "%~dp0Form4Scanner.exe" >nul
rmdir /s /q build 2>nul
del /q Form4Scanner.spec 2>nul

echo.
echo  Done!  Form4Scanner.exe is in this folder:
echo    %~dp0Form4Scanner.exe
echo  You can move it anywhere (e.g. your Desktop) and double-click to run.
echo.
pause
exit /b 0

:fail
echo.
echo  Build failed - see the messages above.
pause
exit /b 1
