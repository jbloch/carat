#!/bin/bash

# Get directory of this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
VENV_DIR="$DIR/.venv"
PYTHON_EXE="$VENV_DIR/bin/python"

# 1. Check for PyCharm's .venv. If missing, check for standard venv.
if [ ! -f "$PYTHON_EXE" ]; then
    VENV_DIR="$DIR/venv"
    PYTHON_EXE="$DIR/venv/bin/python"
fi

# 2. Auto-Bootstrap
if [ ! -f "$PYTHON_EXE" ]; then
    echo "[*] First run detected. Setting up Carat environment..."
    # macOS includes python3 by default
    python3 -m venv "$DIR/.venv"
    if [ $? -ne 0 ]; then
        echo "[!] Failed to create virtual environment. Please check your Python installation."
        exit 1
    fi

    VENV_DIR="$DIR/.venv"
    PYTHON_EXE="$VENV_DIR/bin/python"

    echo "[*] Upgrading pip..."
    "$PYTHON_EXE" -m pip install --upgrade pip >/dev/null

    echo "[*] Installing dependencies..."
    if [ -f "$DIR/requirements.txt" ]; then
        "$PYTHON_EXE" -m pip install -r "$DIR/requirements.txt"
        if [ $? -ne 0 ]; then
            echo "[!] Failed to install requirements."
            exit 1
        fi
    else
        echo "[!] Warning: requirements.txt not found!"
    fi
    echo "[*] Setup complete."
fi

# 3. Launch detached so the terminal can be closed
echo "[*] Starting Carat..."
nohup "$PYTHON_EXE" "$DIR/src/carat_gui.py" >/dev/null 2>&1 &

# Close this specific terminal window automatically using AppleScript
osascript -e 'tell application "Terminal" to close first window' & exit