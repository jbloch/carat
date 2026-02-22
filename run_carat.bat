@echo off
setlocal

:: Get the directory of this batch file
set "DIR=%~dp0"
set "VENV_DIR=%DIR%.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "PYTHONW_EXE=%VENV_DIR%\Scripts\pythonw.exe"

:: 1. Check for PyCharm's .venv. If missing, check for standard venv.
if not exist "%PYTHON_EXE%" (
    set "VENV_DIR=%DIR%venv"
    set "PYTHON_EXE=%DIR%venv\Scripts\python.exe"
    set "PYTHONW_EXE=%DIR%venv\Scripts\pythonw.exe"
)

:: 2. Auto-Bootstrap: If neither exists, build it automatically!
if not exist "%PYTHON_EXE%" (
    echo [*] First run detected. Setting up Carat environment...
    python -m venv "%DIR%.venv"
    if errorlevel 1 goto :error

    :: Update pointers to the newly created .venv
    set "VENV_DIR=%DIR%.venv"
    set "PYTHON_EXE=%DIR%.venv\Scripts\python.exe"
    set "PYTHONW_EXE=%DIR%.venv\Scripts\pythonw.exe"

    echo [*] Upgrading pip...
    "%PYTHON_EXE%" -m pip install --upgrade pip >nul
    if errorlevel 1 goto :error

    echo [*] Installing dependencies...
    if exist "%DIR%requirements.txt" (
        "%PYTHON_EXE%" -m pip install -r "%DIR%requirements.txt"
        if errorlevel 1 goto :error
    ) else (
        echo [!] Warning: requirements.txt not found!
    )
    echo [*] Setup complete.
)

:: 3. Launch windowless and instantly close the terminal
start "" "%PYTHONW_EXE%" "%DIR%src\carat_gui.py"
exit /b

:: 4. Error Handler (Keeps terminal open if things break)
:error
echo.
echo [!] An error occurred during setup. Please check the output above.
pause
exit /b