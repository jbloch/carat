<div align="center">
  <img src="src/assets/carat_logo_icon.png" alt="Carat Logo" width="200"/>
  
  # Carat
  **Diamond-perfect Atmos extraction, processing, and library integration.**
</div>

---

Carat is a remarkably lightweight, completely automated GUI utility designed to ingest Dolby Atmos music releases from practically any format and integrate them directly into your digital library in a single click. 

From the moment a new Blu-ray arrives in your mailbox, Carat handles the entire pipeline—extraction, processing, metadata acquisition, and cover art—with zero friction. If your source files are already on your SSD, it will process an entire release in under a minute (depending on how fast your computer is). If you are starting from a physical disc, the time is limited by the read speed of your optical drive.

<div align="center">
  <img src="docs/carat_screenshot.png" alt="Carat screenshot: Frank Zappa remux in progress" width="600"/>
</div>

## The Universal Atmos Ingestion Tool
Carat is built to handle the chaotic, developing landscape of Atmos release formats. It natively accepts:
* Physical Blu-ray discs
* Blu-ray ISOs and BDMV backup folders
* IAA-style folders (individual MKV or MP4 files per track)
* Headphone Dust-style single MKV files

**The Output:** Regardless of what you feed it, Carat standardizes the output into one uniform, library-ready format: **a single, chapterless `.m4a` file alongside a `.cue` sheet and a `cover.jpg`.** *Why this format?* It is currently the only format that reliably combines true gapless playback with accurate track indexing on media centers like Kodi.

## Intelligent Metadata
You shouldn't have to manually tag your Atmos rips. Carat reaches out to top-tier sources—including [MusicBrainz](https://musicbrainz.org/), the [Cover Art Archive (CAA)](https://coverartarchive.org/), and Apple/iTunes—to automatically pull down high-quality cover art and pristine metadata, and applies smart heuristics to identify and correct imprecise artist or album names, ensuring your library remains perfectly organized.

## Prerequisites
* **[MakeMKV](https://www.makemkv.com/)**: Required to decrypt physical discs or ISOs. *Don't worry if you don't have it yet; the Carat installer will guide you through the installation.*

## Installation (The Near-Zero-Touch Launcher)
Carat uses a highly robust, idempotent launcher script that handles both installation and execution. It is designed to never make a mess of your system. 

1. Download the latest `.zip` release and extract it to a folder.
2. **Windows**: Double-click `carat.bat`
3. **macOS**: Double-click `carat.command`
4. The launcher pretty much takes it from there. The first time you click it, it installs the program and runs it. Subsequently, it just runs it. The installation of some components, especially on Windows, will require you to click through the usual install screens, accepting all the defaults. Windows will ask for permission to install the components before it runs their installers.

**How it works:** On its first run, the launcher automatically checks your system, downloads any missing dependencies (via Winget or Homebrew), builds an isolated Python virtual environment, and launches the GUI. 
*Did something get interrupted?* Just double-click it again! The script is strictly idempotent—you can run it as many times as necessary, and it will simply pick up where it left off without polluting your OS or registry. Subsequent runs will bypass the checks and launch instantly.

Feel free to move the entire Carat-Beta directory anywhere you like, or to rename it; it won't affect the operation of the program.

## Usage
1. Insert your disc or locate your source files.
2. Launch Carat.
3. Select your input, confirm the automatically fetched metadata, and click **Rip Atmos**.
4. **Keep going:** Carat is built for batch processing. You can leave the app open and rip multiple albums in a single session without ever needing to restart. You can clear the Console Output between albums if you like.

## Under the Hood
Instead of relying on bloated frameworks, Carat acts as an elegant conductor for the industry's best open-source media tools. (The 200x200 icon is ten times bigger than the program itself, which is under 1,000 lines of Python.)

* **GUI:** Native [Python](https://www.python.org/)/Tkinter 
* **Extraction:** `makemkvcon` via [MakeMKV](https://www.makemkv.com/)
* **Processing:** `ffmpeg` via [FFmpeg](https://ffmpeg.org/) & `mkvmerge` via [MKVToolNix](https://mkvtoolnix.download/)

## Feedback & Bug Reports

* **Found a bug?** That's not surprising, considering that this is a Beta release. Please open an issue on the [GitHub Issues](link-to-your-issues-tab) page. Including your console log or terminal output helps immensely!
* **Questions or feature ideas?** Join the discussion over on the [QQ Thread](link-to-your-QQ-thread).


---
*Created by Josh Bloch, because he was sick of doing all of this work manually*



