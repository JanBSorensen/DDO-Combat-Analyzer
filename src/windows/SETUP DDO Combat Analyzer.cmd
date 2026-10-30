@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title DDO Combat Analyzer - First-Time Setup

set "APPDIR=%~dp0_internal"

if not exist "%APPDIR%\launch_gui.py" goto not_extracted
if not exist "%APPDIR%\DDO Combat Analyzer.pyw" goto not_extracted

cls
echo ================================================================
echo DDO COMBAT ANALYZER - FIRST-TIME SETUP
echo ================================================================
echo.
echo Setup has 6 steps:
echo   [1/6] Check Windows Package Manager
echo   [2/6] Check Python
echo   [3/6] Check FFmpeg
echo   [4/6] Install core Python packages
echo   [5/6] Install and preload Indra OCR
echo   [6/6] Verify installation and save Python paths
echo.
echo Internet access is required for missing components.
echo Windows may show one or more installer prompts.
echo.
pause

echo.
echo [1/6] Checking Windows Package Manager...
where winget.exe >nul 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: Windows Package Manager ^(winget^) was not found.
    echo Install or update "App Installer" from the Microsoft Store,
    echo then run this setup again.
    echo.
    pause
    exit /b 1
)
echo Windows Package Manager: OK
echo.

set "PYEXE="
set "PYWEXE="

echo [2/6] Checking Python...
where py.exe >nul 2>nul
if not errorlevel 1 (
    for /f "usebackq delims=" %%I in (`py.exe -3.14 -c "import sys; print(sys.executable)" 2^>nul`) do (
        if not defined PYEXE set "PYEXE=%%I"
    )
    if not defined PYEXE (
        for /f "usebackq delims=" %%I in (`py.exe -c "import sys; print(sys.executable)" 2^>nul`) do (
            if not defined PYEXE set "PYEXE=%%I"
        )
    )
)

if not defined PYEXE (
    where python.exe >nul 2>nul
    if not errorlevel 1 (
        for /f "usebackq delims=" %%I in (`python.exe -c "import sys; print(sys.executable)" 2^>nul`) do (
            if not defined PYEXE set "PYEXE=%%I"
        )
    )
)

if defined PYEXE if not exist "!PYEXE!" set "PYEXE="

rem Python may be installed by winget without being on PATH yet. Search the
rem standard per-user and system install locations BEFORE asking winget to install.
if not defined PYEXE (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%ProgramFiles%\Python314\python.exe"
        "%ProgramFiles%\Python313\python.exe"
        "%ProgramFiles%\Python312\python.exe"
        "%ProgramFiles%\Python311\python.exe"
    ) do (
        if not defined PYEXE if exist "%%~P" set "PYEXE=%%~P"
    )
)

if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python" (
    for /r "%LOCALAPPDATA%\Programs\Python" %%P in (python.exe) do (
        if not defined PYEXE set "PYEXE=%%~fP"
    )
)

if not defined PYEXE if exist "%ProgramFiles%" (
    for /d %%D in ("%ProgramFiles%\Python3*") do (
        if not defined PYEXE if exist "%%~fD\python.exe" set "PYEXE=%%~fD\python.exe"
    )
)

if not defined PYEXE (
    echo Python not found. Installing Python 3.14...
    echo.
    winget install --id Python.Python.3.14 --exact --source winget ^
        --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo ERROR: Python installation failed.
        pause
        exit /b 2
    )

    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%~fD\python.exe" set "PYEXE=%%~fD\python.exe"
    )
    for /d %%D in ("%ProgramFiles%\Python3*") do (
        if exist "%%~fD\python.exe" set "PYEXE=%%~fD\python.exe"
    )
)

if not defined PYEXE (
    echo.
    echo ERROR: Python was installed/found, but setup could not locate python.exe.
    echo Close this window, open it again, and rerun setup.
    echo.
    pause
    exit /b 3
)

for %%I in ("!PYEXE!") do set "PYDIR=%%~dpI"
set "PYWEXE=!PYDIR!pythonw.exe"

if not exist "!PYWEXE!" (
    echo.
    echo ERROR: pythonw.exe was not found next to python.exe:
    echo   !PYWEXE!
    echo.
    pause
    exit /b 3
)

echo Python: OK
echo   !PYEXE!
"!PYEXE!" --version
echo.

echo [3/6] Checking FFmpeg...
winget list --id Gyan.FFmpeg --exact >nul 2>nul
if errorlevel 1 (
    echo FFmpeg not found. Installing...
    winget install --id Gyan.FFmpeg --exact --source winget ^
        --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo ERROR: FFmpeg installation failed.
        pause
        exit /b 4
    )
) else (
    echo FFmpeg: OK
)
echo.

echo [4/6] Installing/updating core Python packages...
"!PYEXE!" -m ensurepip --upgrade >nul 2>nul
"!PYEXE!" -m pip install --upgrade pip
if errorlevel 1 echo WARNING: pip upgrade failed. Continuing with the installed pip.
"!PYEXE!" -m pip install --upgrade mss opencv-python numpy
if errorlevel 1 (
    echo.
    echo ERROR: Required Python packages could not be installed.
    pause
    exit /b 5
)
echo Core Python packages: OK
echo.

echo [5/6] Installing and preloading Indra OCR...
"!PYEXE!" -m pip install --upgrade indra-ocr
if errorlevel 1 (
    echo.
    echo ERROR: Indra OCR could not be installed.
    pause
    exit /b 6
)
"!PYEXE!" -c "import indra_ocr; indra_ocr.load(threads=2, mode='ocr'); print('Indra OCR: OK')"
if errorlevel 1 (
    echo.
    echo ERROR: Indra OCR was installed, but its model could not be loaded.
    pause
    exit /b 6
)
echo.

echo [6/6] Verifying installation and saving Python paths...
"!PYEXE!" -c "import tkinter, mss, cv2, numpy, indra_ocr; r=tkinter.Tk(); r.withdraw(); r.update(); r.destroy(); print('GUI prerequisites: OK')"
if errorlevel 1 (
    echo.
    echo ERROR: Python module or GUI verification failed.
    pause
    exit /b 7
)

> "%APPDIR%\python_path.txt" echo !PYEXE!
> "%APPDIR%\pythonw_path.txt" echo !PYWEXE!
echo Saved Python paths: OK
echo.

echo ================================================================
echo SETUP COMPLETE
echo ================================================================
echo.
echo Python:      OK
echo FFmpeg:      OK
echo Indra OCR:   OK
echo Python libs: OK
echo.
echo Starting DDO Combat Analyzer...
start "" "!PYWEXE!" "%APPDIR%\launch_gui.py"
exit /b 0

:not_extracted
echo.
echo ================================================================
echo DDO COMBAT ANALYZER - FILES NOT EXTRACTED
echo ================================================================
echo.
echo Setup cannot run directly from inside the ZIP file.
echo.
echo Please:
echo   1. Close this window.
echo   2. Right-click the downloaded ZIP file.
echo   3. Choose "Extract All...".
echo   4. Open the extracted folder.
echo   5. Run SETUP DDO Combat Analyzer.cmd there.
echo.
pause
exit /b 10
