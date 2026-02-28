#!/bin/bash

# Carat Launcher for macOS - inlcudes intaller, which runs to the extent necessary for a successful launch
# 1. Checks for Homebrew (The "Package Manager")
# 2. Checks for Dependencies (ffmpeg, mkvmerge, python3)
# 3. Sets up virtual environment
# 4. Launches GUI

# Navigate to the script's directory (crucial for double-clicking)
cd "$(dirname "$0")"

echo "[*] CARAT LAUNCHER"
echo "---------------------------------------------------"

# ---------------------------------------------------------
# 0. FIX MACOS PATH (APPLE SILICON & INTEL)
# ---------------------------------------------------------
# When double-clicked, .command scripts don't always load the full user PATH.
# This ensures we can find Homebrew if it's installed.
if [ -x "/opt/homebrew/bin/brew" ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -x "/usr/local/bin/brew" ]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

# ---------------------------------------------------------
# 1. CHECK FOR HOMEBREW
# ---------------------------------------------------------
if ! command -v brew &>/dev/null; then
    echo "[!] Homebrew is missing."
    echo ""
    echo "    To run Carat, you need Homebrew (the standard Mac package manager)."
    echo "    1. Go to https://brew.sh"
    echo "    2. Copy the install command on their page"
    echo "    3. Paste it into this Terminal window and hit Enter"
    echo "    (Note: This may take a few minutes to install Xcode tools)"
    echo ""
    echo "    Once Homebrew is installed, run this Carat script again."
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

if [ $MISSING_TOOLS -eq 1 ]; then
    echo "---------------------------------------------------"
    echo "[!] Failed to install Homebrew dependencies."
    echo "    Please check your internet connection and try again."
    read -n 1 -s -r -p "Press any key to exit..."
    exit 1
fi

# ---------------------------------------------------------
# 3. THE MAKEMKV CONCIERGE
# ---------------------------------------------------------
while [ ! -f "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon" ]; do
    echo ""
    echo "[*] MakeMKV is not installed in your Applications folder."
    echo "    Carat requires MakeMKV to read discs."
    echo "    1. Opening the MakeMKV download page..."
    open https://www.makemkv.com/download/
    echo "    2. Download the Mac OS X version."
    echo "    3. Open the .dmg file and drag MakeMKV to your Applications folder."
    echo ""
    read -p "    Press [Enter] here once MakeMKV is in your Applications folder..."
done

# Add MakeMKV to PATH for this session
export PATH=$PATH:/Applications/MakeMKV.app/Contents/MacOS

# ---------------------------------------------------------
# 4. SETUP PYTHON ENVIRONMENT
# ---------------------------------------------------------
# We use the Homebrew python3 if system python is missing/old
if ! command -v python3 &>/dev/null; then
    echo "[*] Python 3 not found. Installing via Homebrew..."
    brew install python
fi

if [ ! -d ".venv" ]; then
    echo "[*] Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi

# Activate
source .venv/bin/activate

# ---------------------------------------------------------
# 5. INSTALL REQUIREMENTS
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
# 6. LAUNCH GUI
# ---------------------------------------------------------
echo "[*] Launching Carat..."

# Launch in background, suppressing output
nohup python src/carat_gui.py >/dev/null 2>&1 &

# Force Terminal.app to close the active window, ignoring errors
# (in case they somehow launched this from iTerm2)
osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 &

exit 0