#!/bin/bash

# Carat Launcher for macOS
# 1. Checks for Homebrew (The "Package Manager")
# 2. Checks for Dependencies (ffmpeg, mkvmerge, python3)
# 3. Sets up virtual environment
# 4. Launches GUI

# Navigate to the script's directory (crucial for double-clicking)
cd "$(dirname "$0")"

echo "[*] CARAT LAUNCHER"
echo "---------------------------------------------------"

# ---------------------------------------------------------
# 1. CHECK FOR HOMEBREW
# ---------------------------------------------------------
if ! command -v brew &>/dev/null; then
    echo "[!] CRITICAL: Homebrew not found."
    echo ""
    echo "    To run Carat, you need Homebrew (the standard Mac package manager)."
    echo "    1. Go to https://brew.sh"
    echo "    2. Copy the command on the page"
    echo "    3. Paste it into this Terminal window and hit Enter"
    echo "    (Note: This may take a few minutes to install Xcode tools)"
    echo ""
    echo "    Once Homebrew is installed, run this script again."
    echo "---------------------------------------------------"
    read -n 1 -s -r -p "Press any key to exit..."
    exit 1
fi

# ---------------------------------------------------------
# 2. CHECK & INSTALL DEPENDENCIES
# ---------------------------------------------------------
MISSING_TOOLS=0

# Check FFmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo "[*] FFmpeg not found. Installing via Homebrew..."
    brew install ffmpeg
    if [ $? -ne 0 ]; then MISSING_TOOLS=1; fi
fi

# Check MKVToolNix
if ! command -v mkvmerge &>/dev/null; then
    echo "[*] MKVToolNix not found. Installing via Homebrew..."
    brew install mkvtoolnix
    if [ $? -ne 0 ]; then MISSING_TOOLS=1; fi
fi

# Check MakeMKV (App Location)
if [ ! -f "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon" ]; then
    echo "[!] CRITICAL: MakeMKV not found in /Applications."
    echo "    Please install it from makemkv.com"
    MISSING_TOOLS=1
else
    # Add MakeMKV to PATH for this session
    export PATH=$PATH:/Applications/MakeMKV.app/Contents/MacOS
fi

if [ $MISSING_TOOLS -eq 1 ]; then
    echo "---------------------------------------------------"
    echo "[!] Failed to install dependencies."
    echo "    Please fix the errors above and run this script again."
    read -n 1 -s -r -p "Press any key to exit..."
    exit 1
fi

# ---------------------------------------------------------
# 3. SETUP PYTHON ENVIRONMENT
# ---------------------------------------------------------
# We use the Homebrew python3 if system python is missing/old
if ! command -v python3 &>/dev/null; then
    echo "[*] Python 3 not found. Installing via Homebrew..."
    brew install python
fi

if [ ! -d ".venv" ]; then
    echo "[*] Creating virtual environment (.venv)..."
    # Ensure we use the 'python3' we just found/installed
    python3 -m venv .venv
fi

# Activate
source .venv/bin/activate

# ---------------------------------------------------------
# 4. INSTALL REQUIREMENTS
# ---------------------------------------------------------
if [ ! -f ".venv/installed.marker" ]; then
    echo "[*] Installing Python libraries..."
    pip install -q -r requirements.txt
    if [ $? -eq 0 ]; then
        touch .venv/installed.marker
    else
        echo "[!] Failed to install Python requirements."
        read -n 1 -s -r -p "Press any key to exit..."
        exit 1
    fi
fi

# ---------------------------------------------------------
# 5. LAUNCH GUI
# ---------------------------------------------------------
echo "[*] Launching Carat..."

# Launch in background, suppressing output
# nohup allows the terminal to close without killing the app
nohup python src/carat_gui.py >/dev/null 2>&1 &

# Close the terminal window automatically
osascript -e 'tell application "Terminal" to close front window' & exit