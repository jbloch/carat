""" This is essentially a makefile for the carat release artifact(s) """

import os
import shutil

# --- Configuration ---
RELEASE_NAME = "Carat_Beta"
DIST_DIR = "dist"
BUILD_DIR = os.path.join(DIST_DIR, RELEASE_NAME)

# Explicitly list the files and folders you want in the final zip
FILES_TO_INCLUDE = [
    "carat.bat",
    "carat.command",
    "requirements.txt",
    "LICENSE",
    "README.md"  # Add this if you have one!
]
DIRS_TO_INCLUDE = ["src"]


def clean_build_environment():
    """Wipes the old dist folder to ensure a clean build."""
    print(f"[*] Cleaning old build directory: {DIST_DIR}/")
    if os.path.exists(DIST_DIR):
        shutil.rmtree(DIST_DIR)
    os.makedirs(BUILD_DIR)


def copy_release_assets():
    """Copies only the whitelisted files/folders, ignoring python cache."""
    print("[*] Copying release assets...")

    for file in FILES_TO_INCLUDE:
        if os.path.exists(file):
            shutil.copy2(file, BUILD_DIR)
            print(f"  + Copied {file}")
        else:
            print(f"  - Warning: {file} not found, skipping.")

    for d in DIRS_TO_INCLUDE:
        if os.path.exists(d):
            # Extract just the folder name (e.g., 'src') for the destination
            dest_dir = os.path.join(BUILD_DIR, os.path.basename(d))

            shutil.copytree(
                d,
                dest_dir,
                ignore=shutil.ignore_patterns(
                    '__pycache__',
                    '*.pyc',
                    '.DS_Store',
                    'dist',  # <-- Prevents recursion if dist is nested
                    '.venv'  # <-- Prevents copying the massive virtual env
                )
            )
            print(f"  + Copied {os.path.basename(d)}/ directory")


def create_zip_archive():
    """Zips the build directory into a redistributable file."""
    zip_filename = os.path.join(DIST_DIR, RELEASE_NAME)
    print(f"[*] Compressing into {zip_filename}.zip...")

    # make_archive automatically adds the .zip extension
    shutil.make_archive(zip_filename, 'zip', DIST_DIR, RELEASE_NAME)


if __name__ == "__main__":
    print("==================================")
    print("   Building Carat Release Zip")
    print("==================================")

    clean_build_environment()
    copy_release_assets()
    create_zip_archive()

    print("==================================")
    print(f"[SUCCESS] Release ready at: {DIST_DIR}/{RELEASE_NAME}.zip")