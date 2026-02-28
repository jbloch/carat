"""Updates MakeMKV trial key if necessary."""

import json
import os
import platform
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict, Any
from urllib.error import URLError

import logger

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

__author__ = "Joshua Bloch"
__copyright__ = "Copyright 2026, Joshua Bloch"
__license__ = "MIT"
__version__ = "1.0B"

# Anchor to the project root (assuming this script is in src/)
ROOT_DIR: str = str(Path(__file__).resolve().parent.parent)
CONFIG_FILE: str = str(Path(ROOT_DIR) / ".carat_config.json")

# 30 days in seconds (half the lifetime of a MakeMKV trial key)
REFRESH_INTERVAL: int = 30 * 24 * 60 * 60


def load_config() -> Dict[str, Any]:
    """Loads the Carat configuration dictionary from disk; returns an empty dict if not found."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_config(config: Dict[str, Any]) -> None:
    """Saves the given Carat configuration dictionary to disk."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
    except (OSError, TypeError) as e:
        logger.emit(f"[!] Warning: Could not save config file: {e}")


def has_permanent_key() -> bool:
    """Returns True if a paid (non-'T-') MakeMKV key is detected in the OS."""
    if platform.system() == "Windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\MakeMKV") as key:
                existing_key, _ = winreg.QueryValueEx(key, "app_Key")
                # Ensure it's a string before calling startswith
                if existing_key and isinstance(existing_key, str) and not existing_key.startswith("T-"):
                    return True
        except FileNotFoundError:
            pass
    else:
        conf_file = os.path.expanduser("~/.MakeMKV/settings.conf")
        if os.path.exists(conf_file):
            with open(conf_file, "r") as f:
                for line in f:
                    if line.strip().startswith("app_Key"):
                        parts = line.split("=")
                        if len(parts) > 1:
                            existing_key = parts[1].strip().strip('"')
                            if existing_key and not existing_key.startswith("T-"):
                                return True
    return False


def fetch_and_apply_beta_key() -> bool:
    """Scrapes the official MakeMKV forum for the current Beta key and injects it."""
    logger.emit("[*] Fetching the latest MakeMKV Beta Key...")
    url: str = "https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053"

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        html: str = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')

        match = re.search(r'<code>(T-[\w@\-]+)</code>', html)
        if not match:
            logger.emit("[!] Could not find the Beta Key on the forum.")
            return False

        beta_key: str = match.group(1)

        if platform.system() == "Windows":
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\MakeMKV") as key:
                winreg.SetValueEx(key, "app_Key", 0, winreg.REG_SZ, beta_key)
        else:
            conf_dir = os.path.expanduser("~/.MakeMKV")
            os.makedirs(conf_dir, exist_ok=True)
            conf_file = os.path.join(conf_dir, "settings.conf")

            lines = []
            if os.path.exists(conf_file):
                with open(conf_file, "r") as f:
                    lines = f.readlines()

            with open(conf_file, "w") as f:
                key_written: bool = False
                for line in lines:
                    if line.strip().startswith("app_Key"):
                        f.write(f'app_Key = "{beta_key}"\n')
                        key_written = True
                    else:
                        f.write(line)
                if not key_written:
                    f.write(f'app_Key = "{beta_key}"\n')

        logger.emit("[*] MakeMKV Beta Key successfully applied behind the scenes!")
        return True
    except (URLError, OSError) as e:
        logger.emit(f"[!] Error updating MakeMKV key: {e}")
        return False


def main() -> None:
    """Main entry point: checks if the MakeMKV key is valid, and updates the key if necessary."""
    config: Dict[str, Any] = load_config()
    last_checked: float = config.get("makemkv_key_date", 0.0)

    # Check if we are within the 30-days (keys last 60 days)
    if (time.time() - last_checked) < REFRESH_INTERVAL:
        return

    logger.emit("[*] Verifying MakeMKV license status...")
    if has_permanent_key():
        logger.emit("[*] Permanent MakeMKV key detected. Skipping beta key update.")
        # Update timestamp so we don't check the registry every single boot
        config["makemkv_key_date"] = time.time()
        save_config(config)
        return

    # If we made it here, we need a new beta key
    if fetch_and_apply_beta_key():
        config["makemkv_key_date"] = time.time()
        save_config(config)


if __name__ == "__main__":
    main()