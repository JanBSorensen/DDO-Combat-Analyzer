@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APPDIR=%~dp0_internal"

if not exist "%APPDIR%\launch_gui.py" goto not_extracted
if not exist "%APPDIR%\DDO Combat Analyzer.pyw" goto not_extracted

set "PYWEXE="
set "PYEXE="

if exist "%APPDIR%\pythonw_path.txt" (
    set /p PYWEXE=<"%APPDIR%\pythonw_path.txt"
)

if defined PYWEXE (
    if exist "%PYWEXE%" goto launch
)

if exist "%APPDIR%\python_path.txt" (
    set /p PYEXE=<"%APPDIR%\python_path.txt"
)

if defined PYEXE (
    if exist "%PYEXE%" (
        for %%I in ("%PYEXE%") do set "PYWEXE=%%~dpIpythonw.exe"
        if exist "%PYWEXE%" goto launch
    )
)

where pyw.exe >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%I in ('where pyw.exe 2^>nul') do (
        if not defined PYWEXE set "PYWEXE=%%I"
    )
    if defined PYWEXE goto launch
)

echo.
echo Python GUI support could not be found.
echo.
echo Please run:
echo     SETUP DDO Combat Analyzer.cmd
echo.
pause
exit /b 1

:not_extracted
echo.
echo ================================================================
echo DDO COMBAT ANALYZER - FILES NOT EXTRACTED
echo ================================================================
echo.
echo The program cannot run directly from inside the ZIP file.
echo Windows only extracted this CMD file to a temporary folder, so the
echo required _internal files are missing here.
echo.
echo Please:
echo   1. Close this window.
echo   2. Right-click the downloaded ZIP file.
echo   3. Choose "Extract All...".
echo   4. Open the extracted folder.
echo   5. Run START DDO Combat Analyzer.cmd there.
echo.
pause
exit /b 10

:launch
if exist "%APPDIR%\startup_error.log" del /q "%APPDIR%\startup_error.log" >nul 2>nul
start "" "%PYWEXE%" "%APPDIR%\launch_gui.py"
exit /b 0
