@echo off
set "VENV_DIR=%~dp0venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

:: 1. Check for venv
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment not found at: %VENV_DIR%
    echo Please run 'python -m venv venv' and 'pip install -r requirements.txt' first.
    pause
    exit /b 1
)

:: 2. Launch
echo Starting Carat...
"%PYTHON_EXE%" "%~dp0src\carat_gui.py"

:: 3. Catch exit
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Carat crashed with error code %ERRORLEVEL%.
    pause
)
