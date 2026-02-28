"""
Carat - Concise Atmos Ripping Automation Tool.

A GUI tool, command line tool, and library for ripping Dolby Atmos albums (digital and physical) into digital music
libraries, providing gapless playback and track selection. This tool emphasizes ease of use over flexibility. With a
single click, carat automatically gets metadata and cover art from trusted sources (MusicBrainz, CAA, and Apple), and
supports all popular Atmos distribution formats (Blu-ray, mkv, mp4, BDMV).
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

__author__ = "Joshua Bloch"
__copyright__ = "Copyright 2026, Joshua Bloch"
__license__ = "MIT"
__version__ = "1.0B"

import argparse
import atexit
import concurrent.futures
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, NoReturn

import musicbrainzngs as mb

import get_cover_art
import logger

__all__ = ['rip_album_to_library']

import makemkv_updater


# --- (1) Metadata & Utils ---

def seconds_to_cue(seconds: float) -> str:
    """Converts seconds to MM:SS:FF for gapless CUE sheets."""
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}:{int((seconds % 1) * 75):02d}"


def generate_cue_sheet(cue_path: Path, file_name: str, info: dict, chapters: list, mb_tracks: list) -> None:
    """Generates CUE sheet for track indexing into gapless playback."""
    with cue_path.open('w', encoding='utf-8') as f:
        f.write(f'PERFORMER "{info["artist"]}"\nTITLE "{info["title"]} (Atmos)"\nREM DATE {info.get("year", "Unknown")}\nFILE "{file_name}" WAVE\n')
        for i, ch in enumerate(chapters):
            title = mb_tracks[i]['recording']['title'] if i < len(mb_tracks) else f"Track {i + 1}"
            f.write(f'  TRACK {i + 1:02d} AUDIO\n    TITLE "{title}"\n    INDEX 01 {seconds_to_cue(float(ch["start_time"]))}\n')


def _parse_makemkv_msg(line: str) -> str | None:
    """Extracts the human-readable text from MakeMKV MSG lines."""
    if not line.startswith("MSG:"):
        return None

    # Tokenize the CSV-style line, respecting quoted strings
    parts = re.findall(r'[^,"]+|"[^"]*"', line)

    # MakeMKV MSG format: MSG:code,flags,count,formatted_message,template,params...
    # The fully baked, human-readable string is always at index 3.
    if len(parts) >= 4:
        return parts[3].strip('"')

    return None


def _sanitize_filename(name: str) -> str:
    """
    Replaces characters illegal in Windows/Unix filenames with safe alternatives.
    """
    # specific replacement for colons to make "Title: Subtitle" look nice
    name = name.replace(":", " -")
    # Zap standard illegal characters
    return re.sub(r'[\\/*?"<>|]', '_', name).strip()


def _ensure_writable(path: Path) -> None:
    """
    Verifies that the given path exists and is writable by creating and deleting a temp file.
    Raises PermissionError if not writable.
    """
    if not path.exists():
        raise FileNotFoundError(f"Library root does not exist: {path}")

    # We use a localized test file to verify permissions explicitly
    test_file = path / ".carat_write_test"
    try:
        test_file.touch()
        test_file.unlink()
    except OSError:
        raise PermissionError(f"Library root is not writable: {path}")


# --- (2) The Plumbing - subprocess cleanup and output beautification ---

def _process_output_line(line: str, output_acc: list[str], env: dict):
    """Process the given line of output from a MakeMKV subprocess and emit the processed output to the logger."""
    line = line.rstrip('\r\n')
    if not line: return

    output_acc.append(line)

    # Latch Trigger for MakeMKV
    if "PRGC:5017" in line:
        env["is_extracting"] = True

    # [1] MakeMKV Progress
    if "PRG" in line:
        if line.startswith(("PRGV:", "PRGT:")):
            try:
                parts = line.split(":")[1].split(",")
                current, max_val = float(parts[0]), float(parts[2])

                if max_val > 0 and env.get("is_extracting"):
                    pct = (current / max_val) * 100
                    if 0 <= pct <= 100:
                        logger.emit(f"    Atmos Extraction: {pct:.1f}%", is_progress=True)
            except (IndexError, ValueError):
                pass

        env["last_was_progress"] = True
        return

    # [2] ffmpeg Progress
    elif "time=" in line and "speed=" in line:
        try:
            # Parse current time (HH:MM:SS.ms)
            time_str = line.split("time=")[1].split()[0]
            h, m, s = time_str.split(':')
            current_seconds = int(h) * 3600 + int(m) * 60 + float(s)

            clean_stats = line.strip().replace("frame=", "")

            total = env.get("ffmpeg_duration", 0)
            if total > 0:
                pct = (current_seconds / total) * 100
                logger.emit(f"Remuxing: [{pct:.1f}%] {clean_stats}", is_progress=True)
            else:
                logger.emit(f"Remuxing: {clean_stats}", is_progress=True)
        except (ValueError, IndexError):
            pass

        env["last_was_progress"] = True
        return

    # [3] mkvmerge Progress
    elif line.startswith("Progress:"):
        try:
            # mkvmerge outputs lines look like "Progress: 14%"
            pct_str = line.replace("Progress:", "").replace("%", "").strip()
            pct = float(pct_str)
            logger.emit(f"Merging: [{pct:.1f}%]", is_progress=True)
        except ValueError:
            pass

        env["last_was_progress"] = True
        return

    # [4] Normal Output
    else:
        msg = _parse_makemkv_msg(line)
        if msg:
            logger.emit(f"[*] {msg}")
        elif not line.startswith(("DRV:", "TDRV:", "CIDC:", "SINFO:", "TINFO:", "CINFO:")):
            logger.emit(line)

        env["last_was_progress"] = False


def run_command(cmd: list[str], desc: str | None = None, env: dict | None = None) -> str:
    """
    Executes command with live progress updates.
    Includes special handling for MakeMKV progress and ffmpeg status lines.
    Accepts an optional environment dict to pass state (such as album duration) to the output parser.
    This method is aggressively single-threaded. Don't even think about running it in multiple threads.
    """
    global _active_subprocess

    if desc: logger.emit(f"[*] {desc}...")
    logger.emit(f"[*] Command: {cmd}")

    if env is None: env = {}

    # Initialize parser state keys
    env.setdefault("last_was_progress", False)
    env.setdefault("is_extracting", False)

    start_time = time.time()
    hide_console_flag = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                               creationflags=hide_console_flag)  # Suppress console window from popping up on Windows
    _active_subprocess = process

    try:
        output_acc = []
        for line in process.stdout:
            _process_output_line(line, output_acc, env)

        process.wait()
    except:
        # If we are exiting via exception (e.g., Cancel/Ctrl C), kill the process
        process.kill()
        process.wait()
        raise  # Re-raise the exception to let the app handle the crash
    finally:
        _active_subprocess = None  # Whether it succeeded or failed, it's gone

    if process.returncode != 0:
        raise RuntimeError(f"Command failed (Code {process.returncode}): {' '.join(cmd)}")
    emit_summary_log(output_acc, start_time)
    return "\n".join(output_acc)


def emit_summary_log(entire_log: list[Any], start_time: float):
    """Emits to the logger a summary of a completed rip based on the given log and start time."""
    elapsed = time.time() - start_time
    # 1. Search backwards through the accumulated log for ffmpeg's final stats
    final_stats = next((line for line in reversed(entire_log) if "size=" in line and "time=" in line), None)

    if final_stats:
        clean_stats = final_stats.strip().replace("frame=", " ")
        summary = f"[+] Remux finished in {elapsed:.1f}s -> {clean_stats}"
    else:
        summary = f"[+] Task finished in {elapsed:.1f} seconds."

    logger.emit(summary)


# --- (3) Atmos ripping (rips *only* the Atmos stream, fails if there is none ---

def find_primary_title(source_spec: str) -> str:
    """Identifies the main Atmos title in the specified source. Throws exception if no Atmos stream is found."""
    res = run_command([TOOLS.MAKEMKV, "--progress=-stdout", "-r", "info", source_spec, "--minlength=600"],
                      "Surgical Atmos Scan")

    title_scores = {}
    title_chapters = {}

    for line in res.splitlines():
        parts = line.split(",")
        if len(parts) < 4: continue

        try:
            t_idx = parts[0].split(":")[1]
        except IndexError:
            continue

        if line.startswith("TINFO:") and parts[1] == "9" and parts[2] == "4":
            title_chapters[t_idx] = int(parts[3].strip('"'))
            title_scores.setdefault(t_idx, 0)

        if line.startswith("SINFO:"):
            if "A_TRUEHD" in line or "TrueHD Atmos" in line:
                title_scores[t_idx] = max(title_scores.get(t_idx, 0), 1000)
                logger.emit(f"    [*] Title {t_idx}: Found Lossless Atmos (+1000)")
            elif "A_EAC3" in line and "Atmos" in line:
                title_scores[t_idx] = max(title_scores.get(t_idx, 0), 500)
                logger.emit(f"    [*] Title {t_idx}: Found Lossy Atmos (+500)")

    if not title_scores:
        raise RuntimeError("No valid titles found on source.")

    winner = max(title_scores, key=lambda k: (title_scores[k], title_chapters.get(k, 0)))
    winning_score = title_scores[winner]

    logger.emit(f"[*] Scan Results: {json.dumps(title_scores)}")

    if winning_score < 500:
        raise RuntimeError(f"Atmos or Die: Best title ({winner}) has score {winning_score}. No Atmos track found.")

    logger.emit(f"[*] Winner: Title {winner} (Score: {winning_score})")
    return winner


# --- (4) Toolset & Main ---

class Toolset:
    """The collection of underlying AV processing programs that this program depends on."""

    def __init__(self, fatal_error_handler: Callable[[str], None]|None = None) -> None:
        """
        Initializes the toolset, locates executables, and validates the environment. Takes a callback which is
        required to display the fatal error to the user and terminating the application.
        """
        self.IS_WIN = platform.system() == "Windows"

        self.FFMPEG = self._find("ffmpeg",
                                 [r"C:\ffmpeg\bin\ffmpeg.exe", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"])
        self.FFPROBE = self._find("ffprobe",
                                  [r"C:\ffmpeg\bin\ffprobe.exe", "/usr/local/bin/ffprobe", "/opt/homebrew/bin/ffprobe"])
        self.MKVMERGE = self._find("mkvmerge",
                                   [r"C:\Program Files\MKVToolNix\mkvmerge.exe", "/usr/local/bin/mkvmerge",
                                    "/opt/homebrew/bin/mkvmerge"])
        self.MAKEMKV = self._find("makemkvcon64" if self.IS_WIN else "makemkvcon", [
            r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
            "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
            "/usr/bin/makemkvcon"
        ])

        self._validate(fatal_error_handler)

    def _validate(self, fatal_error_handler: Callable[[str], None]|None) -> None:
        """Validates that all required tools exist and are properly licensed.

        Args:
            fatal_error_handler: The callback to trigger on unrecoverable validation failure.
        """
        logger.emit("[*] Validating toolset dependencies...")

        # 1. Update/Validate MakeMKV License first - fails gracefully if offline, due to conservative refresh policy
        makemkv_updater.main()

        # 2. Check for missing binaries
        missing = []
        if not self.FFMPEG: missing.append("FFmpeg")
        if not self.FFPROBE: missing.append("FFprobe")
        if not self.MKVMERGE: missing.append("MKVMerge")
        if not self.MAKEMKV: missing.append("MakeMKV")

        if self.MAKEMKV:
            # Run a dummy command. If the license is dead, it prints the evaluation error.
            res = subprocess.run([self.MAKEMKV, "info", "file:dummy"], capture_output=True, text=True)
            if "Evaluation period has expired" in res.stdout + res.stderr:
                missing.append("MakeMKV (License Expired)")

        # 3. Handle fatal errors
        if missing:
            error_msg = f"Missing required dependencies: {', '.join(missing)}.\n\nPlease ensure they are installed."
            fatal_error_handler(f"[!] {error_msg}")

            if fatal_error_handler:
                # Let the GUI show a nice popup and exit
                fatal_error_handler(error_msg)
            else:
                # Fallback if running headless
                raise RuntimeError(error_msg)

        logger.emit("[*] Toolset validation complete.")

    @staticmethod
    def _find(name: str, prospects: list[str] | None = None) -> str | None:
        # noinspection PyDeprecation
        found = shutil.which(name)
        if found: return found

        if prospects is None: prospects = []
        for p in prospects:
            if Path(p).exists(): return str(Path(p))

        return None

    @staticmethod
    def _trigger_fatal(message: str, handler: Callable[[str], None] | None) -> None:
        """Invokes the injected handler, or falls back to a CLI exit."""
        if handler:
            handler(message)
        else:
            logger.emit(f"FATAL ERROR: {message}")
            sys.exit(1)


# Global singleton placeholder
TOOLS: Toolset | None = None


def init(fatal_error_handler: Callable[[str], None] | None = None) -> None:
    """
    Initializes this module. Must be called by the frontend before ripping. This method ensures that the tools that
    are required for the operation of this module are present and functional. Inf not, it calls the fatal error handler,
    which is responsible for displaying the error to the user and terminating the application.
    """
    global TOOLS
    TOOLS = Toolset(fatal_error_handler)


mb.set_useragent("carat - concise atmos rip automation tool", __version__, "josh@bloch.us")


def rip_stream_to_mkv(src_spec: str, out_path: Path, title_idx: str) -> Path:
    """Rips the indexed stream of the longest title in the specified source to the specified output mkv file."""
    # Force a strict, absolute path to prevent MakeMKV from mixing slashes and backslashes on Windows
    clean_output_path = str(out_path.resolve())
    cmd = [TOOLS.MAKEMKV, "--progress=-stdout", "-r", "mkv", src_spec, title_idx, clean_output_path, "--minlength=600"]
    run_command(cmd, f"Ripping Title {title_idx}")

    mkv_files = list(out_path.glob("*.mkv"))
    if not mkv_files: raise RuntimeError("MakeMKV produced no output.")

    winner = max(mkv_files, key=lambda x: x.stat().st_size)
    for f in mkv_files:
        if f != winner: f.unlink()
    return winner


def find_truehd_stream(mkv_path: Path) -> int | None:
    """
    Returns the index of the TrueHD stream with the most channels from the given mkv file, or None if no TrueHD streams
    are found in the file.
    """
    cmd = [TOOLS.FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index,channels,codec_name",
           "-of", "json", str(mkv_path)]
    res = run_command(cmd, "Scanning for TrueHD Stream")
    try:
        streams = json.loads(res).get('streams', [])
        candidates = [s for s in streams if s.get('codec_name') == 'truehd']
        return int(max(candidates, key=lambda x: int(x.get('channels', 0)))['index']) if candidates else None
    except json.JSONDecodeError:
        return None


def remux_mkv_to_m4a(mkv_path: Path, m4a_path: Path, album_title: str, total_duration: float = 0) -> None:
    """Remuxes the specified mkv file into a chapterless m4a file"""
    idx = find_truehd_stream(mkv_path)
    if idx is None: raise ValueError("No TrueHD stream found.")

    cmd = [
        TOOLS.FFMPEG, "-hide_banner", "-loglevel", "warning", "-stats",
        "-i", str(mkv_path), "-map", f"0:{idx}",
        "-metadata", f"title={album_title}", "-c:a", "copy",
        "-f", "mp4", "-movflags", "+faststart", "-strict", "-2",
        "-fflags", "+genpts", "-map_chapters", "-1", "-y", str(m4a_path)
    ]
    run_command(cmd, "Finalizing Atmos M4A", {"ffmpeg_duration": total_duration})


def extract_chapters_and_duration_from_mkv(mkv_path: Path) -> tuple[list[dict], float]:
    """Returns a list of chapters and the total duration in seconds from the given mkv file."""
    # We add -show_format to get the duration
    cmd = [TOOLS.FFPROBE, "-v", "quiet", "-print_format", "json", "-show_chapters", "-show_format", str(mkv_path)]
    res = run_command(cmd, "Extracting Chapter Markers")
    try:
        data = json.loads(res)
        chapters = data.get('chapters', [])
        duration = float(data.get('format', {}).get('duration', 0))
        return chapters, duration
    except (json.JSONDecodeError, ValueError):
        return [], 0.0


# Maximum number of release groups to search in MusicBrainz when look for the album
MAX_RELEASE_GROUPS: int = 5


def get_metadata_from_musicbrainz(album: str, artist: str, num_tracks: int) -> tuple[dict | None, list | None]:
    """Returns a metadata dictionary for the given release from MusicBrainz, or None if no matching release is found."""
    try:
        rg_res = mb.search_release_groups(artist=artist, release=album)
        for rg in rg_res.get('release-group-list', [])[:MAX_RELEASE_GROUPS]:
            rel_res = mb.browse_releases(release_group=rg['id'], includes=["recordings"])
            for r in rel_res.get('release-list', []):
                if r.get('status') == 'Official':
                    all_tracks = []
                    for m in r.get('medium-list', []): all_tracks.extend(m.get('track-list', []))
                    if len(all_tracks) == num_tracks:
                        return {'title': rg['title'], 'artist': rg.get('artist-credit-phrase', artist),
                                'year': r.get('date', 'Unknown')[:4]}, all_tracks
    except (mb.MusicBrainzError, KeyError, TypeError):
        pass
    return None, None


def merge_folder_to_master_mkv(directory_path: Path, ssd_path: Path) -> Path:
    """
    Merges a directory of sequential audio files (MKV, MKA, M4A, or MP4) into a single master MKV.
    This allows Immersive Audio Album (IAA) track-by-track downloads to be processed as a single album.
    """
    files = sorted(
        [f for f in directory_path.iterdir() if f.is_file() and f.suffix.lower() in ('.mkv', '.mka', '.m4a', '.mp4')])

    if not files:
        raise FileNotFoundError("No valid media files (MKV, MKA, M4A, MP4) found in source folder.")

    out = ssd_path / "master.mkv"
    cmd = [TOOLS.MKVMERGE, "--priority", "lower", "-o", str(out)]

    for i, f in enumerate(files):
        cmd.append(str(f) if i == 0 else f"+{str(f)}")

    # Simple blind append logic for IAA
    run_command(cmd, "Merging IAA Folder")
    return out


# We do all of our work in a temp directory, which will contain a huge MKV. The following code ensures that the
# contents of this directory get deleted, come hell or highwater (though they might survive a BSOD or power outage).
# Similarly, the heavy lifting is done by a background process, and we must track that process so we can kill it
# if the tool dies or is terminated, e.g., by clicking the close button, while a rip is in progress.
TMP_DIR: Path = Path(tempfile.mkdtemp(prefix="carat_"))
_active_subprocess: subprocess.Popen[str] | None = None  # Tracks the currently running tool


def _nuke_dir(path: Path) -> None:
    """ Deletes the given directory with extreme prejudice, even other processes have it locked. """
    for attempt in range(5):  # If at first you don't succeed, try a few more times because Windows is like that
        try:
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                return
        except OSError:
            pass
        time.sleep(0.2)  # Give the OS a moment to release file handles


def clean_up()->None:
    """ Terminates active subprocesses and deletes the tmp directory (idempotent). """
    global _active_subprocess

    # 1. Assassinate the orphaned child process
    if _active_subprocess is not None:
        try:
            _active_subprocess.kill()
            _active_subprocess.wait(timeout=2)  # Give Windows a second to release the file lock
        except OSError:
            pass

    # 2. Nuke the directory now that the locks are gone
    if TMP_DIR.exists():
        _nuke_dir(TMP_DIR)


atexit.register(clean_up) # Ensure _clean_up gets called for all but the most abrupt of process terminations


# Catch OS-level interruptions (Ctrl+C, normal termination signals)
# noinspection PyUnusedLocal
def _signal_handler(signum: object, frame: object) -> NoReturn:
    clean_up()
    os._exit(1)

for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, _signal_handler)
    except ValueError:
        pass


def cleanup_orphaned_temps(min_days_old: int = 1):
    """ Scans sys tmp directory for orphaned carat_ tmp dirs older than the specified number of days & deletes them. """
    temp_root = Path(tempfile.gettempdir())
    now = time.time()
    seconds_limit = min_days_old * 86400

    if not temp_root.exists():
        return

    for carat_tmp_dir in temp_root.glob("carat_*"):
        try:
            age = now - carat_tmp_dir.stat().st_mtime
            if age > seconds_limit:
                _nuke_dir(carat_tmp_dir)
        except (FileNotFoundError, PermissionError):
            pass # Silent failure for cleanup to prevent app startup crashes


def rip_album_to_library(src_path: str, artist: str, album: str, library_root: str) -> None:
    """
    Rips the Atmos stream representing the main title in the specified source into the music library with the
    specified root. The artist and album title are used to obtain metadata and cover art, which are used to
    generate the cue file and cover.jpg in the music library. The library entry consists of a chapterless m4a file
    containing only the Atmos stream, a cue sheet, and a cover.jpg. This format provides gapless playback of the
    entire album, as well as access to individual tracks in Kodi version 21, and is the sole format known to do so.

    This method offers complete ripping of Atmos sources into digital music libraries in a single call, with:

    Polymorphic Input Handling:
      - Integers (e.g. "0", "-1") are Treated as Physical Optical Disc indices
      - .iso files are Mounted virtually and scanned as discs
      - BDMV folders are Scanned as Blu-ray structures
      - .mkv files are Treated as direct sources, bypassing MakeMKV rip (Headphone Dust release format)
      - Standard folders are treated as collections of tracks to be merged into an album (IAA release format)

    The processing pipeline ensures:
      1. A temporary workspace is used for intermediate files
      2. Metadata is fetched from MusicBrainz only if the track count matches what's found on the input source
      3. CUE sheets are generated for gapless playback support
      4. Time-consuming tasks (e.g., Cover Art download, Remuxing) are parallelized where possible
    """
    # 1. Fail Fast: Ensure we can actually write to the library before we start ripping
    lib_path = Path(library_root)
    _ensure_writable(lib_path)

    # 2. Prepare Temp Directory
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    try:
        try:
            # --- CASE A: Physical Disc (Integer Input) ---
            drive_idx = int(src_path)
            if drive_idx == -1:
                res = run_command([TOOLS.MAKEMKV, "-r", "info", "disc:0"])
                if "BD-RE" in res or "BD-ROM" in res: drive_idx = 0

            source_spec = f"disc:{drive_idx}"
            title_idx = find_primary_title(source_spec)
            atmos_mkv = rip_stream_to_mkv(source_spec, TMP_DIR, title_idx)

        except (ValueError, TypeError):
            # Input is not an integer; handle as Path
            src_p = Path(src_path)

            # --- CASE B: ISO Image ---
            if src_p.suffix.lower() == ".iso":
                source_spec = f"iso:{src_p.resolve()}"
                title_idx = find_primary_title(source_spec)
                atmos_mkv = rip_stream_to_mkv(source_spec, TMP_DIR, title_idx)

            # --- CASE C: BDMV Folder ---
            elif src_p.is_dir() and (src_p / "BDMV").exists():
                source_spec = f"file:{src_p.resolve() / 'BDMV'}"
                title_idx = find_primary_title(source_spec)
                atmos_mkv = rip_stream_to_mkv(source_spec, TMP_DIR, title_idx)

            # --- CASE D: Folder of Files (IAA) ---
            elif src_p.is_dir():
                atmos_mkv = merge_folder_to_master_mkv(src_p, TMP_DIR)

            # --- CASE E: Direct MKV ---
            else:
                atmos_mkv = src_p.resolve()
                if not atmos_mkv.exists(): raise FileNotFoundError(f"Not found: {src_path}")

        # 3. Post-Rip Processing & Metadata Fetch
        chapters, duration = extract_chapters_and_duration_from_mkv(atmos_mkv)
        info, tracks = get_metadata_from_musicbrainz(album, artist, len(chapters))
        if info:
            # Canonicalize! Use the official MB names.
            artist = info['artist']
            album = info['title']
            logger.emit(f"[*] Canonicalized as Artist: {artist}, Album: {album}")
        else:
            # Fallback to user input if MB fails
            logger.emit(f"[*] No MusicBrainz metadata for Artist: {artist}, Album: {album}")
            info = {'artist': artist, 'title': album, 'year': 'Unknown'}

        clean_artist = _sanitize_filename(artist)
        clean_album = _sanitize_filename(album)
        logger.emit(f"[*] Sanitized as Artist: {artist}, Album: {album}")

        # 4. Create Final Directory (Now that we have the clean name)
        target = lib_path / clean_artist / f"{clean_album} (Atmos)"
        target.mkdir(parents=True, exist_ok=True)

        # 5. Final Assembly (Concurrent)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            # Pass the (possibly updated) artist/album to cover art search
            cover_future = ex.submit(get_cover_art.download_cover_art, artist, album, target)

            generate_cue_sheet(target / f"{clean_album} (Atmos).cue",
                               f"{clean_album} (Atmos).m4a", info, chapters, tracks or [])
            remux_mkv_to_m4a(atmos_mkv, target / f"{clean_album} (Atmos).m4a", album, duration)

            try:
                cover_future.result(timeout=45)
            except (concurrent.futures.TimeoutError, Exception):
                pass
    finally:
        clean_up()

    logger.emit(f"\n[+] Library Entry Complete: {album}")


def _clean_path_arg(arg: str) -> str:
    """Strips rogue literal quotes caused by Windows shell path escaping (e.g., \\")."""
    return arg.strip('"')


def main():
    """Simple command line tool for carat"""
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("artist")
    parser.add_argument("album")
    parser.add_argument("library_root")
    args = parser.parse_args()

    # Initialize for CLI (no UI handler)
    init()

    rip_album_to_library(_clean_path_arg(args.source), args.artist, args.album, _clean_path_arg(args.library_root))


if __name__ == "__main__":
    main()
