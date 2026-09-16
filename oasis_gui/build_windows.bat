@echo off
REM Build the Windows single-file exe.
REM
REM Requirements (64-bit Windows, Python 3.12):
REM     py -3.12 -m pip install PyQt6==6.6.1 PyQt6-Qt6==6.6.1 ^
REM         PyQt6-Fluent-Widgets curl_cffi PySocks pyinstaller
REM
REM Result: dist\OasisConsole.exe  (single file, writes its config/db/logs
REM next to itself)

setlocal
cd /d "%~dp0"

echo [1/2] installing dependencies
py -3.12 -m pip install --upgrade ^
    "PyQt6==6.6.1" "PyQt6-Qt6==6.6.1" ^
    PyQt6-Fluent-Widgets curl_cffi PySocks pyinstaller || goto :fail

echo [2/2] building
py -3.12 -m PyInstaller --noconfirm --clean OasisConsole.spec || goto :fail

echo.
echo done: dist\OasisConsole.exe
goto :eof

:fail
echo.
echo BUILD FAILED
exit /b 1
