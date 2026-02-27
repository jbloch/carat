@echo off
REM Carat Launcher - Unattended Install Edition
REM 1. Fixes "Delayed Expansion" bug (correctly detects fake Python)
REM 2. Uses Winget for native, silent dependency installation

REM ----------------------------------------------------
REM 0. FIX NETWORK/UNC PATHS
REM ----------------------------------------------------
pushd "%~dp0"
set REQUIRES_RESTART=0

REM ----------------------------------------------------
REM 1. INTELLIGENT MAKEMKV DETECTION
REM ----------------------------------------------------
:CHECK_MAKEMKV
if exist "C:\Program Files (x86)\MakeMKV\makemkvcon64.exe" goto :ADD_MAKEMKV_X86
if exist "C:\Program Files\MakeMKV\makemkvcon64.exe" goto :ADD_MAKEMKV_64

where makemkvcon64 >nul 2>nul
if %errorlevel% equ 0 goto :CHECK_FFMPEG

REM MakeMKV Missing - Concierge Mode
echo.
echo [!] CRITICAL: MakeMKV not found.
echo.
echo     Carat requires MakeMKV to rip discs.
echo     1. I am opening the MakeMKV download page.
echo     2. Download and install the latest version.
echo     3. Once installed, come back here and press any key.
echo.
start https://www.makemkv.com/download/
echo [*] Waiting for you to install MakeMKV...
pause
echo [*] Re-checking system...
goto :CHECK_MAKEMKV

:ADD_MAKEMKV_X86
set "PATH=%PATH%;C:\Program Files (x86)\MakeMKV"
goto :CHECK_FFMPEG

:ADD_MAKEMKV_64
set "PATH=%PATH%;C:\Program Files\MakeMKV"
goto :CHECK_FFMPEG

REM ----------------------------------------------------
REM 2. CHECK & INSTALL FFMPEG
REM ----------------------------------------------------
:CHECK_FFMPEG
where ffmpeg >nul 2>nul
if %errorlevel% equ 0 goto :CHECK_MKVTOOLNIX

echo [*] FFmpeg not found. Auto-installing via Winget...
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
if %errorlevel% neq 0 goto :MANUAL_FFMPEG
set REQUIRES_RESTART=1
goto :CHECK_MKVTOOLNIX

:MANUAL_FFMPEG
echo [!] Please install FFmpeg manually and restart this script.
pause
exit /b 1

REM ----------------------------------------------------
REM 3. CHECK & INSTALL MKVTOOLNIX
REM ----------------------------------------------------
:CHECK_MKVTOOLNIX
where mkvmerge >nul 2>nul
if %errorlevel% equ 0 goto :CHECK_PYTHON

if exist "C:\Program Files\MKVToolNix\mkvmerge.exe" goto :ADD_MKV_64
if exist "C:\Program Files (x86)\MKVToolNix\mkvmerge.exe" goto :ADD_MKV_X86

echo [*] MKVToolNix (mkvmerge) not found. Auto-installing via Winget...
winget install -e --id MoritzBunkus.MKVToolNix --accept-source-agreements --accept-package-agreements
if %errorlevel% neq 0 goto :MANUAL_MKV
set REQUIRES_RESTART=1
goto :CHECK_PYTHON

:ADD_MKV_64
set "PATH=%PATH%;C:\Program Files\MKVToolNix"
goto :CHECK_PYTHON

:ADD_MKV_X86
set "PATH=%PATH%;C:\Program Files (x86)\MKVToolNix"
goto :CHECK_PYTHON

:MANUAL_MKV
echo [!] Please install MKVToolNix manually and restart this script.
pause
exit /b 1

REM ----------------------------------------------------
REM 4. CHECK & INSTALL PYTHON (SHIM-PROOF)
REM ----------------------------------------------------
:CHECK_PYTHON

REM A. Check for 'py' launcher (Always preferred)
where py >nul 2>nul
if %errorlevel% equ 0 set PYTHON_CMD=py
if %errorlevel% equ 0 goto :FOUND_PYTHON

REM B. Check for 'python' executable
where python >nul 2>nul
if %errorlevel% neq 0 goto :INSTALL_PYTHON

REM C. SHIM CHECK: Does 'python --version' actually work?
python --version >nul 2>nul
if %errorlevel% neq 0 goto :INSTALL_PYTHON

REM If we passed both checks, it is a real Python.
set PYTHON_CMD=python
goto :FOUND_PYTHON

:INSTALL_PYTHON
echo [*] Python not found - Auto-installing Python 3.12 via Winget...
winget install -e --id Python.Python.3.12 --scope machine --accept-source-agreements --accept-package-agreements

if %errorlevel% neq 0 goto :MANUAL_PYTHON
set REQUIRES_RESTART=1

set PYTHON_CMD=python
goto :DO_RESTART

:MANUAL_PYTHON
echo [!] Winget install failed. Opening download page...
start https://www.python.org/downloads/windows/
pause
exit /b 1

REM ----------------------------------------------------
REM 5. REFRESH ENVIRONMENT (NO RESTART NEEDED)
REM ----------------------------------------------------
:DO_RESTART
if %REQUIRES_RESTART% neq 1 goto :FOUND_PYTHON

echo [*] Refreshing environment variables...
REM Pull the updated Machine PATH from the registry
for /f "usebackq tokens=2,*" %%A in (`reg query "HKLM\System\CurrentControlSet\Control\Session Manager\Environment" /v Path`) do set "SysPath=%%B"
REM Pull the updated User PATH from the registry
for /f "usebackq tokens=2,*" %%A in (`reg query "HKCU\Environment" /v Path`) do set "UsrPath=%%B"
REM Combine them and set the current session's PATH
set "PATH=%SysPath%;%UsrPath%"
echo [*] Environment refreshed successfully!

:FOUND_PYTHON
REM ----------------------------------------------------
REM 6. SETUP VIRTUAL ENVIRONMENT
REM ----------------------------------------------------

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
REM 8. LAUNCH APPLICATION
REM ----------------------------------------------------
:LAUNCH_APP
echo [*] Launching Carat GUI...
start "" pythonw src\carat_gui.py
popd
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
