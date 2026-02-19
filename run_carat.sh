#!/bin/bash

# Get directory of this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV_DIR="$DIR/venv"
PYTHON_EXE="$VENV_DIR/bin/python"

# 1. Check for venv
if [ ! -f "$PYTHON_EXE" ]; then
    echo "[ERROR] Virtual environment not found at: $VENV_DIR"
    echo "Please run 'python3 -m venv venv' and 'pip install -r requirements.txt' first."
    exit 1
fi

# 2. Launch
echo "Starting Carat..."
"$PYTHON_EXE" "$DIR/src/carat_gui.py"
