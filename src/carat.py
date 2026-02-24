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
from typing import Any, Callable

import musicbrainzngs as mb

import get_cover_art
import logger


# --- (1) Metadata & Utils ---

def seconds_to_cue(seconds: float) -> str:
    """Converts seconds to MM:SS:FF for gapless CUE sheets."""
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}:{int((seconds % 1) * 75):02d}"


def generate_cue_sheet(cue_path: Path, m4a_name: str, info: dict, chapters: list, mb_tracks: list) -> None:
    """Generates the CUE sheet for gapless playback."""
    with cue_path.open('w', encoding='utf-8') as f:
        f.write(
            f'PERFORMER "{info["artist"]}"\nTITLE "{info["title"]} (Atmos)"\nREM DATE {info.get("year", "Unknown")}\nFILE "{m4a_name}" WAVE\n')
        for i, ch in enumerate(chapters):
            title = mb_tracks[i]['recording']['title'] if i < len(mb_tracks) else f"Track {i + 1}"
            f.write(
                f'  TRACK {i + 1:02d} AUDIO\n    TITLE "{title}"\n    INDEX 01 {seconds_to_cue(float(ch["start_time"]))}\n')


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


# --- (2) The Plumbing - subprocess cleanup and output beautification ---

def _process_output_line(line: str, output_acc: list[str], state: dict, log_callback: Callable | None):
    line = line.rstrip('\r\n')
    if not line: return

    output_acc.append(line)

    # Latch Trigger. We do not report MakeMKV progress until it starts the main extraction
    if "PRGC:5017" in line:
        state["is_extracting"] = True

    # [1] MakeMKV Progress
    if "PRG" in line:
        if line.startswith(("PRGV:", "PRGT:")):
            try:
                parts = line.split(":")[1].split(",")
                current, max_val = float(parts[0]), float(parts[2])

                if max_val > 0 and state.get("is_extracting"):
                    pct = (current / max_val) * 100
                    if 0 <= pct <= 100:
                        logger.emit(f"    Atmos Extraction: {pct:.1f}%", log_callback, is_progress=True)
            except (IndexError, ValueError):
                pass

        state["last_was_progress"] = True
        return

    # [2] ffmpeg Progress
    elif "size=" in line and "time=" in line and "bitrate=" in line:
        clean_stats = line.strip().replace("frame=", " ")
        logger.emit(f"Transcoding: {clean_stats}", log_callback, is_progress=True)

        state["last_was_progress"] = True
        return

    # [3] Normal Output
    else:
        msg = _parse_makemkv_msg(line)
        if msg:
            logger.emit(f"[*] {msg}", log_callback)
        elif not line.startswith(("DRV:", "TDRV:", "CIDC:", "SINFO:", "TINFO:", "CINFO:")):
            logger.emit(line, log_callback)

        state["last_was_progress"] = False


def run_command(cmd: list[str], desc: str | None = None, log_callback: Callable | None = None) -> str:
    """
    Executes command with live progress updates.
    Includes special handling for MakeMKV progress and ffmpeg status lines.
    This method is aggressively single-threaded. Don't even think about running it in multiple threads.
    """
    global _active_subprocess

    if desc: logger.emit(f"[*] {desc}...", log_callback)
    logger.emit(f"[*] Command: {cmd}", log_callback)

    # text=True handles decoding; bufsize=1 ensures line-buffered output
    start_time = time.time()
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    _active_subprocess = process  # So we can kill the process from outside this method if it all goes south

    try:
        output_acc = []
        parser_state = {
            "last_was_progress": False,
            "is_extracting": False
        }
        for line in process.stdout:
            _process_output_line(line, output_acc, parser_state, log_callback)

        process.wait()
    finally:
        _active_subprocess = None

    if process.returncode != 0:
        raise RuntimeError(f"Command failed (Code {process.returncode}): {' '.join(cmd)}")
    emit_summary_log(output_acc, start_time, log_callback)
    return "\n".join(output_acc)


def emit_summary_log(output_acc: list[Any], start_time: float, log_callback: Callable[..., Any] | None):
    elapsed = time.time() - start_time
    # 1. Search backwards through the accumulated log for ffmpeg's final stats
    final_stats = next((line for line in reversed(output_acc) if "size=" in line and "time=" in line), None)

    if final_stats:
        clean_stats = final_stats.strip().replace("frame=", " ")
        summary = f"[+] Transcode finished in {elapsed:.1f}s -> {clean_stats}"
    else:
        summary = f"[+] Task finished in {elapsed:.1f} seconds."

    logger.emit(summary, log_callback)


# --- (3) Atmos ripping (rips *only* the Atmos stream, fails if there is none ---

def find_primary_title(source_spec: str, log_callback: Callable | None = None) -> str:
    """Identifies the Atmos title. Fails if no Atmos track is found."""
    res = run_command([TOOLS.MAKEMKV, "--progress=-stdout", "-r", "info", source_spec, "--minlength=600"],
                      "Surgical Atmos Scan", log_callback)

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
                logger.emit(f"    [*] Title {t_idx}: Found Lossless Atmos (+1000)", log_callback)
            elif "A_EAC3" in line and "Atmos" in line:
                title_scores[t_idx] = max(title_scores.get(t_idx, 0), 500)
                logger.emit(f"    [*] Title {t_idx}: Found Lossy Atmos (+500)", log_callback)

    if not title_scores:
        raise RuntimeError("No valid titles found on source.")

    winner = max(title_scores, key=lambda k: (title_scores[k], title_chapters.get(k, 0)))
    winning_score = title_scores[winner]

    logger.emit(f"[*] Scan Results: {json.dumps(title_scores)}", log_callback)

    if winning_score < 500:
        raise RuntimeError(f"Atmos or Die: Best title ({winner}) has score {winning_score}. No Atmos track found.")

    logger.emit(f"[*] Winner: Title {winner} (Score: {winning_score})", log_callback)
    return winner


# --- (4) Toolset & Main ---

class Toolset:
    def __init__(self, fatal_error_handler: Callable[[str], None] | None = None) -> None:
        self.IS_WIN = platform.system() == "Windows"
        self.FFMPEG = self._find("ffmpeg",
                                 [r"C:\ffmpeg\bin\ffmpeg.exe", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"])
        self.FFPROBE = self._find("ffprobe",
                                  [r"C:\ffmpeg\bin\ffprobe.exe", "/usr/local/bin/ffprobe", "/opt/homebrew/bin/ffprobe"])
        self.MKVMERGE = self._find("mkvmerge", [r"C:\Program Files\MKVToolNix\mkvmerge.exe", "/usr/local/bin/mkvmerge",
                                                "/opt/homebrew/bin/mkvmerge"])
        self.MAKEMKV = self._find("makemkvcon64" if self.IS_WIN else "makemkvcon", [
            r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
            "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
            "/usr/bin/makemkvcon"
        ])
        self._validate(fatal_error_handler)

    @staticmethod
    def _find(name: str, prospects: list[str] | None = None) -> str | None:
        # noinspection PyDeprecation
        found = shutil.which(name)
        if found: return found

        if prospects is None: prospects = []
        for p in prospects:
            if Path(p).exists(): return str(Path(p))

        return None

    def _validate(self, error_handler: Callable[[str], None] | None) -> None:
        # 1. Check for missing dependencies
        missing = [k for k, v in self.__dict__.items() if v is None and not isinstance(v, bool) and k != 'IS_WIN']
        if missing:
            msg = f"Missing dependencies: {', '.join(missing)}\nPlease install them or check your system paths."
            self._trigger_fatal(msg, error_handler)

        # 2. Validate MakeMKV License
        try:
            # 'info dev:all' triggers the license check without ripping anything
            result = subprocess.run(
                [self.MAKEMKV, "info", "dev:all"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            output = (result.stdout + result.stderr).lower()

            if "expired" in output or "too old" in output or "evaluation" in output:
                msg = "MakeMKV beta key appears to be expired or invalid.\nPlease open the MakeMKV GUI, enter the latest beta key from the forums, and try again."
                self._trigger_fatal(msg, error_handler)

        except Exception as e:
            self._trigger_fatal(f"Failed to validate MakeMKV: {e}", error_handler)

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


def init_toolset(error_handler: Callable[[str], None] | None = None) -> None:
    """Instantiates the toolset. Must be called by the frontend before ripping."""
    global TOOLS
    TOOLS = Toolset(error_handler)


mb.set_useragent("carat - concise atmos rip automation tool", __version__, "josh@bloch.us")


def rip_atmos_to_master_mkv(source_spec: str, mkv_path: Path, title_idx: str = "all",
                            log_callback: Callable | None = None) -> Path:
    # Force a strict, absolute path to prevent MakeMKV from mixing slashes and backslashes on Windows
    clean_mkv_path = str(mkv_path.resolve())
    cmd = [TOOLS.MAKEMKV, "--progress=-stdout", "-r", "mkv", source_spec, title_idx, clean_mkv_path, "--minlength=600"]
    run_command(cmd, f"Ripping Title {title_idx}", log_callback)

    mkv_files = list(mkv_path.glob("*.mkv"))
    if not mkv_files: raise RuntimeError("MakeMKV produced no output.")

    winner = max(mkv_files, key=lambda x: x.stat().st_size)
    for f in mkv_files:
        if f != winner: f.unlink()
    return winner


def find_truehd_stream(mkv_path: Path, log_callback: Callable | None = None) -> int | None:
    cmd = [TOOLS.FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index,channels,codec_name",
           "-of", "json", str(mkv_path)]
    res = run_command(cmd, "Scanning for TrueHD Stream", log_callback)
    try:
        streams = json.loads(res).get('streams', [])
        candidates = [s for s in streams if s.get('codec_name') == 'truehd']
        return int(max(candidates, key=lambda x: int(x.get('channels', 0)))['index']) if candidates else None
    except json.JSONDecodeError:
        return None


def transcode_mkv_to_m4a(mkv_path: Path, m4a_path: Path, album_title: str,
                         log_callback: Callable | None = None) -> None:
    idx = find_truehd_stream(mkv_path, log_callback)
    if idx is None: raise ValueError("No TrueHD stream found.")

    cmd = [
        TOOLS.FFMPEG, "-hide_banner", "-loglevel", "warning", "-stats",
        "-i", str(mkv_path), "-map", f"0:{idx}",
        "-metadata", f"title={album_title}", "-c:a", "copy",
        "-f", "mp4", "-movflags", "+faststart", "-strict", "-2",
        "-fflags", "+genpts", "-map_chapters", "-1", "-y", str(m4a_path)
    ]
    run_command(cmd, "Finalizing Atmos M4A", log_callback)


def extract_chapters_from_mkv(mkv_path: Path, log_callback: Callable | None = None) -> list[dict]:
    cmd = [TOOLS.FFPROBE, "-v", "quiet", "-print_format", "json", "-show_chapters", str(mkv_path)]
    res = run_command(cmd, "Extracting Chapter Markers", log_callback)
    try:
        return json.loads(res).get('chapters', [])
    except json.JSONDecodeError:
        return []


# Maximum number of release groups to search in MusicBrainz when look for the album
MAX_RELEASE_GROUPS: int = 5


def get_metadata_from_musicbrainz(album: str, artist: str, num_tracks: int) -> tuple[dict | None, list | None]:
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
    except Exception:
        pass
    return None, None


def merge_folder_to_master_mkv(directory_path: Path, ssd_path: Path, log_callback: Callable | None = None) -> Path:
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
    run_command(cmd, "Merging IAA Folder", log_callback)
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
        except Exception:
            pass
        time.sleep(0.2)  # Give the OS a moment to release file handles


def _clean_up():
    """ Terminates active subprocesses and deletes the tmp directory (idempotent). """
    global _active_subprocess

    # 1. Assassinate the orphaned child process
    if _active_subprocess is not None:
        try:
            _active_subprocess.kill()
            _active_subprocess.wait(timeout=2)  # Give Windows a second to release the file lock
        except Exception:
            pass

    # 2. Nuke the directory now that the locks are gone
    if TMP_DIR.exists():
        _nuke_dir(TMP_DIR)


atexit.register(_clean_up) # Ensure _clean_up gets called for all but the most abrupt of process terminations


# Catch OS-level interruptions (Ctrl+C, normal termination signals)
# noinspection PyUnusedLocal
def _signal_handler(signum, frame):
    _clean_up()
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
        except Exception:
            pass # Silent failure for cleanup to prevent app startup crashes

def rip_album_to_library(src_path: str, artist: str, album: str, library_root: str,
                         log_callback: Callable | None = None) -> None:
    """
    Orchestrates the rip/transcode pipeline for a single release.

    Polymorphic Input Handling:
      - Integers (e.g. "0", "-1"): Treated as Physical Optical Disc indices.
      - .iso files: Mounted virtually and scanned as discs.
      - BDMV folders: Scanned as Blu-ray structures.
      - .mkv files: Treated as direct sources (bypassing MakeMKV rip).
      - Standard folders: Treated as IAA (Immersive Audio Album) collections to be merged.

    The pipeline ensures:
      1. A temporary workspace is used for intermediate files.
      2. Metadata is fetched from MusicBrainz based on track counts.
      3. CUE sheets are generated for gapless playback support.
      4. Heavy lifting (Cover Art download, Transcoding) is parallelized where possible.
    """
    # 1. Prepare the destination directory, and ensure tmp directory exists
    target = Path(library_root) / artist / f"{album} (Atmos)"
    target.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)  # Normally a no-op, but recreates directory if this is not the first rip

    try:
        try:
            # --- CASE A: Physical Disc (Integer Input) ---
            drive_idx = int(src_path)

            # Auto-detect drive if -1 is passed (Heuristic: Look for BD-RE/BD-ROM)
            if drive_idx == -1:
                res = run_command([TOOLS.MAKEMKV, "-r", "info", "disc:0"])
                if "BD-RE" in res or "BD-ROM" in res: drive_idx = 0

            # Execute "Atmos or Die" Scan & Rip
            source_spec = f"disc:{drive_idx}"
            title_idx = find_primary_title(source_spec, log_callback)
            atmos_mkv = rip_atmos_to_master_mkv(source_spec, TMP_DIR, title_idx, log_callback)

        except (ValueError, TypeError):
            # Input is not an integer; handle as Path
            src_p = Path(src_path)

            # --- CASE B: ISO Image ---
            if src_p.suffix.lower() == ".iso":
                source_spec = f"iso:{src_p.resolve()}"
                title_idx = find_primary_title(source_spec, log_callback)
                atmos_mkv = rip_atmos_to_master_mkv(source_spec, TMP_DIR, title_idx, log_callback)

            # --- CASE C: Mounted Blu-ray or other BDMV Folder Structure ---
            elif src_p.is_dir() and (src_p / "BDMV").exists():
                source_spec = f"file:{src_p.resolve() / 'BDMV'}"
                title_idx = find_primary_title(source_spec, log_callback)
                atmos_mkv = rip_atmos_to_master_mkv(source_spec, TMP_DIR, title_idx, log_callback)

            # --- CASE D: Folder of MKVs, such as IAA distribution  (Merge MKVs or MP4s rather than ripping) ---
            elif src_p.is_dir():
                atmos_mkv = merge_folder_to_master_mkv(src_p, TMP_DIR, log_callback)

            # --- CASE E: Direct MKV File, such as Headphone Dust distribution (Bypass Rip; input is master MKV) ---
            else:
                atmos_mkv = src_p.resolve()
                if not atmos_mkv.exists(): raise FileNotFoundError(f"Not found: {src_path}")

        # 3. Post-Rip Processing
        # Extract chapter markers from the master MKV (essential for CUE sheet)
        chaps = extract_chapters_from_mkv(atmos_mkv, log_callback)

        # Fetch metadata (Track titles, Year) from MusicBrainz
        info, tracks = get_metadata_from_musicbrainz(album, artist, len(chaps))
        info = info or {'artist': artist, 'title': album, 'year': 'Unknown'}

        # 4. Final Assembly (Concurrent)
        # We run the Cover Art download in parallel with the heavy Transcode
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            cover_future = ex.submit(get_cover_art.get_cover_art, artist, album, target, log_callback)

            # Generate CUE Sheet
            generate_cue_sheet(target / f"{album} (Atmos).cue", f"{album} (Atmos).m4a", info, chaps, tracks or [])

            # Transcode Master MKV, whose chapters are the TrueHD/Atmos songs to a chapterless M4A Container
            transcode_mkv_to_m4a(atmos_mkv, target / f"{album} (Atmos).m4a", album, log_callback)

            # Ensure Cover Art finished (soft timeout)
            try:
                cover_future.result(timeout=45)
            except Exception:
                pass
    finally:
        _clean_up()

    logger.emit(f"\n[+] Library Entry Complete: {album}", log_callback)


def _clean_path_arg(arg: str) -> str:
    """Strips rogue literal quotes caused by Windows shell path escaping (e.g., \\")."""
    return arg.strip('"')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("artist")
    parser.add_argument("album")
    parser.add_argument("library_root")
    args = parser.parse_args()

    # Initialize for CLI (no UI handler)
    init_toolset()

    rip_album_to_library(
        _clean_path_arg(args.source),
        args.artist,
        args.album,
        _clean_path_arg(args.library_root)
    )


if __name__ == "__main__":
    main()
