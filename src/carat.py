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
__version__ = "1.0B2.1"

__all__ = ['rip_album_to_library']

import argparse
import atexit
import concurrent.futures
import difflib
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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NoReturn

import musicbrainzngs as mb

import get_cover_art
import logger
import makemkv_updater
# noinspection PyProtectedMember
from get_cover_art import normalize_for_fuzzy_comparison


# --- (1) Metadata & Utils ---

def seconds_to_cue(seconds: float) -> str:
    """Converts seconds to MM:SS:FF for gapless CUE sheets."""
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}:{int((seconds % 1) * 75):02d}"


def generate_cue_sheet(cue_path: Path, file_name: str, info: dict, chapters: list, mb_tracks: list) -> None:
    """Generates CUE sheet for track indexing into gapless playback."""
    with cue_path.open('w', encoding='utf-8') as f:
        f.write(
            f'PERFORMER "{info["artist"]}"\nTITLE "{info["title"]} (Atmos)"\nREM DATE {info.get("year", "Unknown")}\nFILE "{file_name}" WAVE\n')
        for i, ch in enumerate(chapters):
            title = mb_tracks[i]['title'] if i < len(mb_tracks) else f"Track {i + 1}"
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


def _sanitize_filename(name: str) -> str:
    """
    Replaces characters illegal in Windows/Unix filenames with safe alternatives.
    """
    # specific replacement for colons to make "Title: Subtitle" look nice
    name = name.replace(":", " -")

    # Zap standard illegal characters
    name = re.sub(r'[\\/*?"<>|]', '_', name).strip()

    # Strip all leading and trailing underscores
    name = name.strip('_')

    # Fallback to a single underscore if the entire string was stripped away
    return name if name else "_"


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

@dataclass
class TitleInfo:
    """Holds the parsed MakeMKV state for a single title."""
    score: int = 0
    chapters: int = 0
    size: int = 0
    file_name: str = "Unknown"
    duration: str = "Unknown"
    size_str: str = "Unknown"
    streams: list[str] = field(default_factory=list)

def parse_makemkv_info(res: str) -> dict[str, TitleInfo]:
    """Parses the raw text output of 'makemkvcon info' into a dictionary of TitleInfo objects."""
    titles = defaultdict(TitleInfo)

    for line in res.splitlines():
        parts = line.split(",")
        if len(parts) < 4: continue

        try:
            t_idx = parts[0].split(":")[1]
        except IndexError:
            continue

        # MakeMKV TINFO Format: TINFO:title_idx,attribute_id,code,"value"
        if line.startswith("TINFO:"):
            attr_id = parts[1]
            code = parts[2]
            val = parts[3].strip('"') if len(parts) > 3 else ""

            if code == "0":
                if attr_id == "8":
                    titles[t_idx].chapters = int(val)
                elif attr_id == "9":
                    titles[t_idx].duration = val
                elif attr_id == "10":
                    titles[t_idx].size_str = val
                elif attr_id == "11":
                    titles[t_idx].size = int(val)
                elif attr_id == "27":
                    titles[t_idx].file_name = val

        # MakeMKV SINFO Format: SINFO:title_idx,stream_idx,attribute_id,code,"value"
        if line.startswith("SINFO:"):
            if len(parts) >= 5:
                attr_id = parts[2]
                val = parts[4].strip('"')
                if attr_id == "30":
                    titles[t_idx].streams.append(val)

            # Keep existing score logic (removed redundant inline logging)
            if "A_TRUEHD" in line or "TrueHD Atmos" in line:
                titles[t_idx].score = max(titles[t_idx].score, 1000)
            elif "A_EAC3" in line and "Atmos" in line:
                titles[t_idx].score = max(titles[t_idx].score, 500)

    return titles


def log_disc_topology(titles: dict[str, TitleInfo]) -> None:
    """Pretty-prints the disc topology parsed from MakeMKV."""
    logger.emit("\n[*] === DISC TOPOLOGY SCAN ===")

    if not titles:
        logger.emit("    [!] No valid titles found during scan.")
        return

    # Sort by integer title ID for clean output
    for t_idx, info in sorted(titles.items(), key=lambda x: int(x[0])):
        logger.emit(f"    [Title {t_idx}] {info.file_name}")
        logger.emit(f"      - Duration: {info.duration} ({info.chapters} Chapters)")
        logger.emit(f"      - Size: {info.size_str}")

        if info.streams:
            logger.emit("      - Streams:")
            for i, stream in enumerate(info.streams, start=1):
                # Add a visual star for the Atmos streams so they pop in the log
                marker = "★" if "Atmos" in stream or "TrueHD" in stream else "->"
                logger.emit(f"          {marker} Stream {i}: {stream}")
        else:
            logger.emit("      - Streams: None detected")

        logger.emit("")  # Blank line between titles


def get_best_mb_candidate(target_artist: str, target_album: str, chapter_count: int,
                          candidates: list[dict]) -> dict | None:
    """
    Finds the best MusicBrainz candidate for a given target and chapter count.
    Filters for exact matches or +1 preamble matches, prioritizing exact matches,
    then string similarity to the target artist and album.
    """
    if not candidates:
        return None

    # Filter for valid matches (exact or +1 preamble)
    matched = [c for c in candidates if 0 <= (chapter_count - len(c['tracks'])) <= 1]

    if matched:
        safe_target = normalize_for_fuzzy_comparison(f"{target_artist} {target_album}").replace(" ", "")

        def get_similarity(c: dict) -> float:
            """Similarity scorer function for album and artist titles"""
            safe_cand = normalize_for_fuzzy_comparison(f"{c['artist']} {c['title']}").replace(" ", "")
            return difflib.SequenceMatcher(None, safe_target, safe_cand).ratio()

        # Sort by: 1. Track diff (ascending), 2. Similarity (descending)
        matched.sort(key=lambda c: (chapter_count - len(c['tracks']), -get_similarity(c)))

        logger.emit(f"    [*] MB Candidate Scoreboard for {chapter_count} tracks:")
        for c in matched:
            diff = chapter_count - len(c['tracks'])
            sim = get_similarity(c)
            logger.emit(f"        -> [Δ={diff}, Sim={sim:.2f}] {c['artist']} - {c['title']}")

        winner = matched[0]
        logger.emit(f"    [+] Selected MB Candidate: {winner['artist']} - {winner['title']}")
        return winner

    return None


def find_primary_title(source_spec: str, artist: str, album: str) -> tuple[str, dict | None]:
    """
    Identifies the (likely) main Atmos title by "intersecting" MakeMKV and MusicBrainz metadata.
    Fetches the MusicBrainz candidates concurrently while MakeMKV scans the disc.

    Returns:
        A tuple containing:
        - winner_idx (str): The MakeMKV title index of the correct Atmos track (e.g., "0").
        - matched_candidate (dict | None): The exact MusicBrainz metadata dictionary that validated
          the winning title, or None if the network fetch failed/timed out.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as prefetch_ex:
        # 1. Fire off the network fetch in the background
        candidates_future = prefetch_ex.submit(fetch_candidate_metadata, artist, album)

        # 2. Scan input using MakeMKV locally (runs concurrently with the fetch)
        res = run_command([TOOLS.MAKEMKV, "--progress=-stdout", "-r", "info", source_spec, "--minlength=600"],
                          "Atmos Scan")
        titles = parse_makemkv_info(res)
        log_disc_topology(titles)
        if not any(info.score > 0 for info in titles.values()):
            raise RuntimeError("No valid Atmos titles found on source.")

        # 3. Synchronize: Grab the results of the background fetch
        candidates = []
        try:
            candidates = candidates_future.result(timeout=10) or []
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            logger.emit("    [!] Warning: MusicBrainz candidate pre-fetch timed out or was cancelled.")

    # Find all Atmos input titles with the same track count as a candidate MusicBrainz releases
    if candidates:
        valid_titles = []
        for t_idx, info in titles.items():
            if info.score <= 0:
                logger.emit(f"    [-] Rejected Title {t_idx} (No Atmos stream detected)")
                continue
            best_candidate = get_best_mb_candidate(artist, album, info.chapters, candidates)

            # Keep the title if it matched a candidate, OR if MB is entirely offline
            if best_candidate or not candidates:
                valid_titles.append((t_idx, best_candidate))
    else:
        # Graceful degradation if MB is down/offline
        valid_titles = [(t_idx, None) for t_idx, info in titles.items() if info.score > 0]
    if not valid_titles:
        raise RuntimeError("No titles in the input matched the expected track counts from MusicBrainz.")

    # noinspection PyShadowingNames
    def sort_key(item: tuple[str, dict | None]) -> tuple[int, int, int]:
        """Sort criterion to rank multiple matches: (MB Relevance Rank, track-count difference, -Size)"""
        t_idx, matched_candidate = item

        # 1. Relevance: Index in the MusicBrainz search results (0 is best, 999 if MB is offline)
        rank = candidates.index(matched_candidate) if matched_candidate in candidates else 999

        # 2. Track-count accuracy: How close is the physical chapter count to the logical track count? (0 or 1)
        diff = abs(titles[t_idx].chapters - len(matched_candidate['tracks'])) if matched_candidate else 0

        # 3. Size: Negated so that larger files sort first when using min()
        size = titles[t_idx].size

        return rank, diff, -size

    logger.emit("[*] Evaluated Heuristic Scores (MusicBrainz Rank, Track Count Accuracy, File Size):")
    for vt in valid_titles:
        rank, diff, neg_size = sort_key(vt)
        logger.emit(f"    [-] Title {vt[0]}: MB Rank={rank}, Track Count Δ={diff}, Size={-neg_size} bytes")

    # Pick the winner that scores lowest (best) across the 3-tier hierarchy
    winner_tuple = min(valid_titles, key=sort_key)
    winner_idx = winner_tuple[0]
    matched_candidate = winner_tuple[1]

    w_rank, w_diff, w_neg_size = sort_key(winner_tuple)
    logger.emit(
        f"[*] Winner: Title {winner_idx} (Rank: {w_rank}, Track count Δ: {w_diff}, Size: {-w_neg_size} bytes, Score: {titles[winner_idx].score})")

    return winner_idx, matched_candidate


# --- (4) Toolset & Main ---

class Toolset:
    """The collection of underlying AV processing programs that this program depends on."""

    def __init__(self, fatal_error_handler: Callable[[str], None] | None = None) -> None:
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

    def _validate(self, fatal_error_handler: Callable[[str], None] | None) -> None:
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


def rip_title_to_mkv(src_spec: str, out_path: Path, title_idx: str) -> Path:
    """Rips the specified title from the source into a single MKV container file."""
    # Force a strict, absolute path to prevent MakeMKV from mixing slashes and backslashes on Windows
    clean_output_path = str(out_path.resolve())
    cmd = [TOOLS.MAKEMKV, "--progress=-stdout", "-r", "mkv", src_spec, title_idx, clean_output_path, "--minlength=600"]

    start_time = time.time()
    run_command(cmd, f"Ripping Title {title_idx}")
    elapsed = time.time() - start_time

    mkv_files = list(out_path.glob("*.mkv"))
    if not mkv_files:
        raise RuntimeError("MakeMKV produced no output.")
    if len(mkv_files) > 1:
        # This shouldn't happen in a clean temp dir, but it's good to know if it does!
        logger.emit(f"[!] Warning: MakeMKV produced {len(mkv_files)} files. Using the first one.")
    winner = mkv_files[0]

    size_mb = winner.stat().st_size / (1024 * 1024)
    logger.emit(
        f"[+] Title extraction complete: {size_mb:.1f} MB in {elapsed:.1f} seconds (Avg: {size_mb / elapsed:.1f} MB/s)")

    return winner


def find_atmos_stream(mkv_path: Path, preferred_codec: str = "truehd") -> int | None:
    """
    Returns the index of the highest quality Atmos stream based on the preferred_codec,
    with appropriate fallbacks and warnings.
    """
    cmd = [TOOLS.FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index,channels,codec_name",
           "-of", "json", str(mkv_path)]
    res = run_command(cmd, "Scanning for Atmos Stream")
    try:
        streams = json.loads(res).get('streams', [])

        # 1. Hunt for the user's explicit preference first
        preferred_candidates = [s for s in streams if s.get('codec_name') == preferred_codec]
        if preferred_candidates:
            return int(max(preferred_candidates, key=lambda x: int(x.get('channels', 0)))['index'])

        # 2. The IAA Fallback: If caller wanted TrueHD but it's not there, grab E-AC-3
        if preferred_codec == "truehd":
            eac3_candidates = [s for s in streams if s.get('codec_name') == "eac3"]
            if eac3_candidates:
                logger.emit("[!] =========================================================")
                logger.emit("[!] WARNING: No TrueHD found! Falling back to lossy EAC3-JOC.")
                logger.emit("[!] =========================================================")
                return int(max(eac3_candidates, key=lambda x: int(x.get('channels', 0)))['index'])

        # 3. The Absolute Fallback: Basic AC-3 (5.1)
        ac3_candidates = [s for s in streams if s.get('codec_name') == "ac3"]
        if ac3_candidates:
            logger.emit("[!!!] ======================================================================")
            logger.emit("[!!!] WARNING: NO ATMOS METADATA DETECTED! Falling back to 5.1 channel AC-3.")
            logger.emit("[!!!] ======================================================================")

            return int(max(ac3_candidates, key=lambda x: int(x.get('channels', 0)))['index'])

        return None
    except json.JSONDecodeError:
        return None


def remux_mkv_to_m4a(mkv_path: Path, m4a_path: Path, album_title: str, total_duration: float = 0) -> None:
    """Remuxes the specified mkv file into a chapterless m4a file"""
    idx = find_atmos_stream(mkv_path)
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


# The maximum number of releases to search on MusicBrainz for a good match for the user-supplied album and artist names
MAX_RELEASES_TO_SEARCH: int = 15


def fetch_candidate_metadata(artist: str, album: str) -> list[dict[str, Any]]:
    """
    Returns the metadata for the candidate releases corresponding to the given (inexact) artist and album name.
    All the releases returned will come from the same release group, and each will have a different track-count
    from the others. (The assumption is that all members of a release group with the same track-count will
    have the same track sequence, so we only need one release per track count, and it doesn't matter which one.)
    """
    logger.emit("\n[*] === STARTING METADATA FETCH (MULTI-EDITION SEARCH) ===")

    rg_id, rg_artist, rg_title = find_release_group(album, artist)
    if not rg_id:
        logger.emit("    [-] No matching release group found. Aborting metadata fetch.")
        return []

    releases = find_releases_and_dates_for_release_group(rg_id, rg_title)
    logger.emit(
        f"    [+] Found {len(releases)} matching releases for {rg_artist} - {rg_title} (RG ID: {rg_id})."
    )
    if not releases:
        logger.emit("    [-] No matching releases found. Aborting metadata fetch.")
        return []

    candidates = fetch_tracklists_for_releases(releases, rg_id, rg_artist, rg_title)
    logger.emit(f"    [+] Metadata fetch complete. Returning {len(candidates)} candidates.")
    return candidates


def find_release_group(album: str, artist: str) -> tuple[str | None, str | None, str | None]:
    """
        Finds the release group corresponding to the given album and artist name (which may be inexact).
        Searches by release rather than release group to bypass strict artist indexing. Returns the release group id,
        followed by the artist, followed by the title (or None, None, None if no matching release group could be found).
    """
    rg_id, rg_title, rg_artist = None, None, None

    for is_strict in [True, False]:
        query = f'artist:"{artist}" AND release:"{album}"' if is_strict else f'"{artist}" "{album}"'
        logger.emit(f"    [*] Executing Query: {query}")
        try:
            res = mb.search_releases(query=query, limit=MAX_RELEASES_TO_SEARCH)
            for r in res.get('release-list', []):
                found_artist = extract_artist_from_musicbrainz_metadata(r)
                found_album = r.get('title', 'Unknown')

                if _is_safe_match(artist, found_artist) and _is_safe_match(album, found_album):
                    rg_id = r.get('release-group', {}).get('id')
                    rg_title, rg_artist = found_album, found_artist
                    logger.emit(f"    [+] Match Found: {found_artist} - {found_album} (RG ID: {rg_id})")
                    break
        except mb.WebServiceError as e:
            logger.emit(f"    [!] API Error: {e}")
        if rg_id:
            break
    return rg_id, rg_artist, rg_title


def find_releases_and_dates_for_release_group(rg_id: str, rg_title: str) -> list[tuple[str, str]]:
    """
    Returns release IDs and dates of editions of the given release group corresponding to all possible track-counts.
    Evaluates mediums individually to strictly match physical disc topology.
    """
    logger.emit(f"[*] Fetching all editions and media for Release Group: {rg_title}")

    releases = []
    limit = 100
    offset = 0

    # 1. Fetch ALL editions by paginating through the browse_releases endpoint
    try:
        while True:
            result = mb.browse_releases(release_group=rg_id, includes=['media'], limit=limit, offset=offset)
            batch = result.get('release-list', [])
            releases.extend(batch)

            if len(batch) < limit:
                break  # We've reached the end of the list
            offset += limit

        logger.emit(f"[+] API returned {len(releases)} editions for '{rg_title}'.")
    except mb.WebServiceError as e:
        logger.emit(f"[!] Error fetching releases: {e}")
        return []

    unique_releases = {}
    seen_counts = set()

    # 2. Map the physical mediums to find unique track counts
    for r in releases:
        mediums = r.get('medium-list', [])

        if not mediums:
            t_count = int(r.get('medium-track-count', r.get('track-count', 0)))
            if t_count > 0 and t_count not in seen_counts:
                seen_counts.add(t_count)
                unique_releases[r['id']] = r.get('date', '')[:4]
            continue

        for m in mediums:
            m_count = int(m.get('track-count', 0))
            if m_count > 0 and m_count not in seen_counts:
                seen_counts.add(m_count)
                unique_releases[r['id']] = r.get('date', '')[:4]

    logger.emit(f"[+] Identified unique track counts: {sorted(list(seen_counts))}")
    return [(r_id, date) for r_id, date in unique_releases.items()]


def fetch_tracklists_for_releases(release_ids_and_dates: list[tuple[str, str]],
                                  rg_id: str, rg_artist: str, rg_title: str) -> list[dict[str, Any]]:
    """Fetch the tracklists for the given release ids (and dates), which pertain to the given release group metadata"""
    logger.emit(f"    [*] Fetching tracklists for {len(release_ids_and_dates)} editions...")
    candidates = []
    for rel_id, year in release_ids_and_dates:
        try:
            rel_info = mb.get_release_by_id(rel_id, includes=['recordings'])

            # Treat EVERY medium as its own independent candidate
            for medium in rel_info.get('release', {}).get('medium-list', []):
                medium_tracks = []
                for track in medium.get('track-list', []):
                    medium_tracks.append({
                        'title': track.get('recording', {}).get('title', 'Unknown Track'),
                        'duration': track.get('recording', {}).get('length', 0)
                    })

                if medium_tracks:
                    candidates.append({
                        'title': rg_title,
                        'artist': rg_artist,
                        'year': year or 'Unknown',
                        'mbid': rg_id,
                        'tracks': medium_tracks
                    })
        except mb.WebServiceError:
            continue
    return candidates


def extract_artist_from_musicbrainz_metadata(entity: dict) -> str:
    """
    Reconstructs the full artist credit string from MusicBrainz's parsed list format. MusicBrainz stores
    collaborations as lists of fragments (e.g., [{'name': 'Simon'}, {'joinphrase': ' & '}, {'name': 'Garfunkel'}]).
    """
    # Sometimes older API endpoints just return a flat string
    credit = entity.get('artist-credit', '')
    if isinstance(credit, str):
        return credit

    if isinstance(credit, list):
        full_name = ""
        for fragment in credit:
            if isinstance(fragment, dict):
                # 'name' is the literal text on the jacket; 'artist' is the DB entity
                name = fragment.get('name') or fragment.get('artist', {}).get('name', '')
                join_phrase = fragment.get('joinphrase', '')
                full_name += name + join_phrase
            elif isinstance(fragment, str):
                full_name += fragment
        return full_name.strip() or "Unknown"

    return "Unknown"


def _is_safe_match(expected: str, found: str) -> bool:
    """
    Compares two strings for similarity after stripping all spaces and punctuation.
    Prevents false negatives from stylized acronyms (e.g., 'REM' vs. 'R.E.M.') while
    guarding against completely mismatched albums.
    """
    safe_expected = normalize_for_fuzzy_comparison(expected).replace(" ", "")
    safe_found = normalize_for_fuzzy_comparison(found).replace(" ", "")

    ratio = difflib.SequenceMatcher(None, safe_expected, safe_found).ratio()
    return ratio > 0.7


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

    # Global options: Output file and chapter generation strategy
    cmd = [
        TOOLS.MKVMERGE,
        "--priority", "lower",
        "-o", str(out),
        "--generate-chapters", "when-appending"
    ]

    # Input options: Strip existing chapters from every incoming file, then append
    for i, f in enumerate(files):
        cmd.append("--no-chapters")
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


def clean_up() -> None:
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


atexit.register(clean_up)  # Ensure _clean_up gets called for all but the most abrupt of process terminations


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
            pass  # Silent failure for cleanup to prevent app startup crashes


def get_mkv_master_file_and_metadata(src_path: str, artist: str, album: str) -> tuple[
    Path, dict[str, Any] | None, list[dict[str, Any]], float]:
    """
    Acquires the master MKV file, extracts its chapters/duration, and fetches the matching MusicBrainz metadata.
    """
    src_p = Path(src_path)
    source_spec = None

    # 1. Identify Source Type and Resolve source_spec
    try:
        drive_idx = int(src_path)
        if drive_idx == -1:
            res = run_command([TOOLS.MAKEMKV, "-r", "info", "disc:0"])
            if "BD-RE" in res or "BD-ROM" in res: drive_idx = 0
        source_spec = f"disc:{drive_idx}"
    except (ValueError, TypeError):
        if src_p.suffix.lower() == ".iso":
            source_spec = f"iso:{src_p.resolve()}"
        elif src_p.is_dir() and (src_p / "BDMV").exists():
            source_spec = f"file:{src_p.resolve() / 'BDMV'}"

    # 2. Execute Source-Specific Acquisition
    if source_spec:
        # --- Handle MakeMKV Supported Formats: Blu-ray, Blu-ray iso, and BDMV folder ---
        title_idx, matched_candidate = find_primary_title(source_spec, artist, album)
        atmos_mkv = rip_title_to_mkv(source_spec, TMP_DIR, title_idx)
        chapters, duration = extract_chapters_and_duration_from_mkv(atmos_mkv)
    else:
        # --- Handle other formats ---
        if src_p.is_dir():  # Folder of mkv or mp4 files (IAA)
            atmos_mkv = merge_folder_to_master_mkv(src_p, TMP_DIR)
        else:  # Single MKV file (Headphone Dust)
            atmos_mkv = src_p.resolve()
            if not atmos_mkv.exists():
                raise FileNotFoundError(f"Not found: {src_path}")

        # Intersect local MKV chapters with MusicBrainz candidates
        chapters, duration = extract_chapters_and_duration_from_mkv(atmos_mkv)
        candidates = fetch_candidate_metadata(artist, album)
        matched_candidate = get_best_mb_candidate(artist, album, len(chapters), candidates)

        # Fallback: if no strict match was found but we HAVE candidates, just blindly trust the top result
        if not matched_candidate and candidates:
            matched_candidate = candidates[0]

    return atmos_mkv, matched_candidate, chapters, duration


def rip_album_to_library(src_path: str, artist: str, album: str, library_root: str, output_container: str = ".m4a",
                         preferred_codec: str = "truehd") -> None:
    """
    Rips the Atmos stream representing the main title in the specified source into the music library with the
    specified root. The artist and album title are used to obtain metadata and cover art, which are used to
    generate the cue file and cover.jpg in the music library. The library entry consists of a chapterless audio
    file (M4A or MKV) containing only the Atmos stream, a cue sheet, and a cover.jpg. This format provides gapless
    playback of the entire album, as well as access to individual tracks, and is the only format known to do so
    on most platforms.

    The output codec will be the highest quality codec consistent with the caller's preferred codec. The three
    possibilities, in order of decreasing quality, are TrueHD Atmos (lossless), E-AC-3-JOC Atmos (lossy), and AC-3
    Surround (not Atmos!). The permitted values for preferred_codec are "truehd" (default), "eac3", and "ac3".

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
      3. CUE sheets are generated and internal chapters are stripped for gapless playback support
      4. Time-consuming tasks (e.g., Cover Art download, Remuxing) are parallelized where possible
    """

    lib_path = Path(library_root)
    _ensure_writable(lib_path)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # 1. MKV Acquisition Phase
        atmos_mkv, matched_candidate, chapters, duration = get_mkv_master_file_and_metadata(src_path, artist, album)

        # 2. Canonicalization & Sanitization of artist and album title
        info = matched_candidate or {}
        tracks = info.get('tracks', [])
        mbid = info.get('mbid')

        if matched_candidate:
            canonicalized_artist = info.get('artist', artist)
            canonicalized_album = info.get('title', album)
            if (artist != canonicalized_artist) or (album != canonicalized_album):
                logger.emit(f"[*] Canonicalized as Artist: {canonicalized_artist}, Album: {canonicalized_album}")
                artist = canonicalized_artist
                album = canonicalized_album
        else:
            logger.emit(f"[*] No MusicBrainz metadata for Artist: {artist}, Album: {album}")
            info = {'artist': artist, 'title': album, 'year': 'Unknown'}

        clean_artist = _sanitize_filename(artist)
        clean_album = _sanitize_filename(album)
        if (clean_artist != artist) or (clean_album != album):
            logger.emit(f"[*] Sanitized as Artist: {clean_artist}, Album: {clean_album}")
        dest = lib_path / clean_artist / f"{clean_album} (Atmos)"
        dest.mkdir(parents=True, exist_ok=True)

        # 3. Final Assembly (Concurrent)
        idx = find_atmos_stream(atmos_mkv, preferred_codec)
        if idx is None:
            raise ValueError("No compatible audio stream (TrueHD, E-AC-3, or AC-3) found in master file.")

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            cover_future = ex.submit(get_cover_art.download_cover_art, artist, album, dest, mbid)

            # 1. Generate the cue sheet, injecting the correct target extension
            final_audio_name = f"{clean_album} (Atmos){output_container}"
            if info and (tracks := info.get('tracks')):
                chapters = chapters[:len(tracks)]   # Eliminate final "ghost chapter" if it exists
            generate_cue_sheet(dest / f"{clean_album} (Atmos).cue", final_audio_name, info, chapters, tracks)

            # 2. If we are building a video file for a car, wait for the cover art now
            has_cover_art = False
            if output_container == ".mp4":
                logger.emit("    [*] Checking cover art status for video stream...")
                try:
                    # Give it a 5-second grace period if it hasn't finished during the MakeMKV scan
                    cover_future.result(timeout=5)
                    has_cover_art = (dest / "cover.jpg").exists()
                    if has_cover_art:
                        logger.emit("    [+] Cover art ready. Building visual audio track.")
                except concurrent.futures.TimeoutError:
                    logger.emit("    [!] Cover art fetch timed out. Falling back to black screen.")
                except Exception as e:
                    logger.emit(f"    [!] Cover art fetch failed ({type(e).__name__}). Falling back to black screen.")

            # 3. Build container-specific FFmpeg remuxing command and execute it
            if output_container == ".mp4": # Nominal video, and appropriate "branding" to satisfy hardware decoders
                cmd = [TOOLS.FFMPEG, "-hide_banner", "-loglevel", "warning", "-stats"]

                if has_cover_art:
                    cmd.extend(["-loop", "1", "-framerate", "1", "-i", str(dest / "cover.jpg")])
                else:
                    cmd.extend(["-f", "lavfi", "-i", "color=c=black:s=720x480:r=1"])

                cmd.extend([
                    "-probesize", "100M", "-analyzeduration", "100M",
                    "-i", str(atmos_mkv),
                    "-map", "0:v", "-map", f"1:{idx}",
                    "-metadata", f"title={album}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-shortest",
                    "-brand", "mp42", "-f", "mp4", "-movflags", "+faststart", "-strict", "-2"
                ])
            else:  # m4a or mkv; audio only
                cmd = [
                    TOOLS.FFMPEG, "-hide_banner", "-loglevel", "warning", "-stats",
                    "-probesize", "100M", "-analyzeduration", "100M",
                    "-i", str(atmos_mkv), "-map", f"0:{idx}",
                    "-metadata", f"title={album}", "-c:a", "copy"
                ]
                if output_container == ".m4a":
                    cmd.extend(["-f", "mp4", "-movflags", "+faststart", "-strict", "-2"])
                else:
                    cmd.extend(["-f", "matroska"])

            cmd.extend(["-fflags", "+genpts", "-map_chapters", "-1", "-y", str(dest / final_audio_name)])
            run_command(cmd, f"Finalizing Atmos {output_container[1:].upper()}", {"ffmpeg_duration": duration})

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
    parser = argparse.ArgumentParser(description="Rip and remux Dolby Atmos albums.")
    parser.add_argument("source", help="Source (Disc index, ISO, or Folder)")
    parser.add_argument("artist", help="Album artist")
    parser.add_argument("album", help="Album title")
    parser.add_argument("library_root", help="Destination music library root")

    # Optional flags for format selection
    parser.add_argument("--output-container", choices=["m4a", "mp4", "mkv"], default="m4a",
                        help="Output container format (default: m4a)")
    parser.add_argument("--preferred-codec", choices=["truehd", "eac3", "ac3"], default="truehd",
                        help="Preferred Atmos audio codec (default: truehd)")

    args = parser.parse_args()

    # Initialize for CLI (no UI handler)
    init()

    rip_album_to_library(
        _clean_path_arg(args.source),
        args.artist,
        args.album,
        _clean_path_arg(args.library_root),
        output_container=f".{args.output_container}",
        preferred_codec=args.preferred_codec
    )

if __name__ == "__main__":
    main()
