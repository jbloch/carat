@echo off
REM Carat Launcher - Winget & Dependency Aware (Polished)

set REQUIRES_RESTART=0

REM ----------------------------------------------------
REM 1. INTELLIGENT MAKEMKV DETECTION
REM ----------------------------------------------------
if exist "C:\Program Files (x86)\MakeMKV\makemkvcon64.exe" (
    set "PATH=%PATH%;C:\Program Files (x86)\MakeMKV"
    goto :CHECK_FFMPEG
)

if exist "C:\Program Files\MakeMKV\makemkvcon64.exe" (
    set "PATH=%PATH%;C:\Program Files\MakeMKV"
    goto :CHECK_FFMPEG
)

where makemkvcon64 >nul 2>nul
if %errorlevel% equ 0 goto :CHECK_FFMPEG

echo [!] CRITICAL: MakeMKV not found.
echo     Please install MakeMKV from makemkv.com.
pause
exit /b 1

REM ----------------------------------------------------
REM 2. CHECK & INSTALL FFMPEG
REM ----------------------------------------------------
:CHECK_FFMPEG
where ffmpeg >nul 2>nul
if %errorlevel% equ 0 goto :CHECK_MKVMERGE

echo [!] FFmpeg not found.
choice /M "    Would you like to auto-install FFmpeg via Winget?"
if %errorlevel% neq 1 goto :MANUAL_FFMPEG

echo [*] Installing FFmpeg...
winget install -e --id Gyan.FFmpeg
if %errorlevel% neq 0 goto :MANUAL_FFMPEG
set REQUIRES_RESTART=1
goto :CHECK_MKVMERGE

:MANUAL_FFMPEG
echo [!] Please install FFmpeg manually and restart this script.
pause
exit /b 1

REM ----------------------------------------------------
REM 3. INTELLIGENT MKVTOOLNIX DETECTION (NEW!)
REM ----------------------------------------------------
:CHECK_MKVMERGE
REM Check PATH first
where mkvmerge >nul 2>nul
if %errorlevel% equ 0 goto :CHECK_PYTHON

REM Check Default Install Location (64-bit)
if exist "C:\Program Files\MKVToolNix\mkvmerge.exe" (
    echo [*] Found MKVToolNix in Program Files.
    set "PATH=%PATH%;C:\Program Files\MKVToolNix"
    goto :CHECK_PYTHON
)

REM Check Default Install Location (32-bit/Legacy)
if exist "C:\Program Files (x86)\MKVToolNix\mkvmerge.exe" (
    echo [*] Found MKVToolNix in Program Files (x86).
    set "PATH=%PATH%;C:\Program Files (x86)\MKVToolNix"
    goto :CHECK_PYTHON
)

REM If we get here, it's truly missing
echo [!] MKVToolNix (mkvmerge) not found.
choice /M "    Would you like to auto-install MKVToolNix via Winget?"
if %errorlevel% neq 1 goto :MANUAL_MKV

echo [*] Installing MKVToolNix...
winget install -e --id MoritzBunkus.MKVToolNix
if %errorlevel% neq 0 goto :MANUAL_MKV
set REQUIRES_RESTART=1
goto :CHECK_PYTHON

:MANUAL_MKV
echo [!] Please install MKVToolNix manually and restart this script.
pause
exit /b 1

REM ----------------------------------------------------
REM 4. HANDLE RESTART IF NEEDED
REM ----------------------------------------------------
:CHECK_PYTHON
if %REQUIRES_RESTART% equ 1 (
    echo.
    echo [!] Tools installed successfully!
    echo     Please CLOSE this window and run carat.bat again to refresh the PATH.
    pause
    exit /b 0
)

REM ----------------------------------------------------
REM 5. DETECT PYTHON
REM ----------------------------------------------------
where py >nul 2>nul
if %errorlevel% equ 0 set PYTHON_CMD=py
if %errorlevel% equ 0 goto :FOUND_PYTHON

where python >nul 2>nul
if %errorlevel% equ 0 set PYTHON_CMD=python
if %errorlevel% equ 0 goto :FOUND_PYTHON

echo [!] CRITICAL ERROR: Python not found.
pause
exit /b 1

:FOUND_PYTHON
REM ----------------------------------------------------
REM 6. SETUP VIRTUAL ENVIRONMENT
REM ----------------------------------------------------
if exist .venv goto :ACTIVATE_ENV
echo [*] Creating virtual environment (.venv)...
%PYTHON_CMD% -m venv .venv
if %errorlevel% neq 0 goto :ERROR_VENV

:ACTIVATE_ENV
if not exist .venv\Scripts\activate.bat goto :ERROR_BROKEN_VENV
call .venv\Scripts\activate.bat

REM ----------------------------------------------------
REM 7. INSTALL DEPENDENCIES
REM ----------------------------------------------------
if exist .venv\installed.marker goto :LAUNCH_APP
echo [*] Checking dependencies...
pip install -q -r requirements.txt
if %errorlevel% neq 0 goto :ERROR_INSTALL
echo. > .venv\installed.marker

REM ----------------------------------------------------
REM 8. LAUNCH APPLICATION (GHOST MODE)
REM ----------------------------------------------------
:LAUNCH_APP
echo [*] Launching Carat GUI...
start "" pythonw src\carat_gui.py
exit /b 0

REM ----------------------------------------------------
REM ERROR HANDLERS
REM ----------------------------------------------------
:ERROR_VENV
echo [!] Failed to create virtual environment.
pause
exit /b 1

:ERROR_BROKEN_VENV
echo [!] The .venv folder exists but appears broken.
pause
exit /b 1

:ERROR_INSTALL
echo [!] Failed to install requirements.
pause
exit /b 1