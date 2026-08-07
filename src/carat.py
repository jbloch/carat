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
__version__ = "1.0B3"

__all__ = ['rip_album_to_library', 'Container', 'Codec']

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
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, NoReturn, NamedTuple, cast

import musicbrainzngs as mb
# noinspection PyProtectedMember
from mutagen.flac import FLAC, Picture

import get_cover_art
import logger
import makemkv_updater
# noinspection PyProtectedMember
from get_cover_art import normalize_for_fuzzy_comparison, retry_mb_api


# --- (1) Metadata & Utils ---

def seconds_to_cue(seconds: float) -> str:
    """Converts seconds to MM:SS:FF for gapless CUE sheets."""
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}:{int((seconds % 1) * 75):02d}"


def generate_cue_sheet(cue_path: Path, file_name: str, info: dict, chapters: list, mb_tracks: list) -> None:
    """Generates CUE sheet for track indexing into gapless playback."""
    with cue_path.open('w', encoding='utf-8') as f:
        f.write(f'PERFORMER "{info["artist"]}"\nTITLE "{info["title"]} (Atmos)"\nREM DATE {info.get("year", "Unknown")}\nFILE "{file_name}" WAVE\n')
        for i, ch in enumerate(chapters):
            title = mb_tracks[i]['title'] if i < len(mb_tracks) else f"Track {i + 1}"
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
    """Process the given line of output from a subprocess and emit the processed output to the logger."""
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
                        logger.emit(f"    Extraction: {pct:.1f}%", is_progress=True)
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
            offset = env.get("ffmpeg_time_offset", 0.0)

            if total > 0:
                pct = ((current_seconds + offset) / total) * 100
                pct = min(pct, 100.0)  # Clamp to 100% just in case

                # Allow dynamic prefix for different stages (Slicing vs. Remuxing)
                prefix = env.get("ffmpeg_prefix", "Remuxing")
                logger.emit(f"{prefix}: [{pct:.1f}%] {clean_stats}", is_progress=True)
            else:
                prefix = env.get("ffmpeg_prefix", "Remuxing")
                logger.emit(f"{prefix}: {clean_stats}", is_progress=True)
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
        elif not line.startswith(("DRV:", "TDRV:", "CIDC:", "SINFO:", "TINFO:", "CINFO:", "TCOUNT:")):
            logger.emit(line)

        env["last_was_progress"] = False


def run_command(cmd: list[str], desc: str | None = None, env: dict | None = None, suppress_summary: bool = False) -> str:
    """
    Synchronously executes command with live progress updates.
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
        if process.stdout:  # Will always be satisfied, but PyCharm doesn't know it
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
        out_str = "\n".join(output_acc).lower()
        if "disk full" in out_str or "no space left" in out_str:
            raise RuntimeError("The process failed because a drive ran out of disk space. Please free up space.")
        raise RuntimeError(f"Command failed (Code {process.returncode}): {' '.join(cmd)}")

    if not suppress_summary:
        emit_summary_log(output_acc, start_time, env)
    return "\n".join(output_acc)


def emit_summary_log(entire_log: list[str], start_time: float, env: dict | None = None):
    """Emits to the logger a summary of a completed task based on the given log and start time."""
    elapsed = time.time() - start_time
    # 1. Search backwards through the accumulated log for ffmpeg's final stats
    final_stats = next((line for line in reversed(entire_log) if "size=" in line and "time=" in line), None)

    if final_stats:
        clean_stats = final_stats.strip().replace("frame=", " ")
        action = "Slice" if env and env.get("ffmpeg_prefix") == "Slicing" else "Remux"
        summary = f"[+] {action} finished in {elapsed:.1f}s -> {clean_stats}"
    else:
        summary = f"[+] Task finished in {elapsed:.1f} seconds."

    logger.emit(summary)
    logger.emit("") # Blank line visually separates tasks


@dataclass
class TitleInfo:
    """Holds the parsed MakeMKV state for a single title."""
    score: int = 0
    chapters: int = 0
    size: int = 0
    file_name: str = "Unknown"
    duration: str = "Unknown"
    duration_sec: float = 0.0
    size_str: str = "Unknown"
    streams: dict[int, str] = field(default_factory=dict)
    atmos_streams: set[int] = field(default_factory=set)


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

        if line.startswith("TINFO:"):
            attr_id = parts[1]
            code = parts[2]
            val = parts[3].strip('"') if len(parts) > 3 else ""
            if code == "0":
                if attr_id == "8": titles[t_idx].chapters = int(val)
                elif attr_id == "9":
                    titles[t_idx].duration = val
                    try:
                        h, m, s = val.split(':')
                        titles[t_idx].duration_sec = int(h) * 3600 + int(m) * 60 + int(s)
                    except ValueError: pass
                elif attr_id == "10": titles[t_idx].size_str = val
                elif attr_id == "11": titles[t_idx].size = int(val)
                elif attr_id == "27": titles[t_idx].file_name = val

        if line.startswith("SINFO:"):
            if len(parts) >= 5:
                stream_idx = int(parts[1])
                attr_id = parts[2]
                val = parts[4].strip('"')

                # If ANY attribute for this stream contains "atmos", flag its absolute index!
                if "atmos" in val.lower():
                    titles[t_idx].atmos_streams.add(stream_idx)

                # Attribute 30 is the human-readable stream description
                if attr_id == "30":
                    titles[t_idx].streams[stream_idx] = val
                    match = re.search(r'(\d)\.(\d)', val)
                    if match:
                        channels = int(match.group(1)) + int(match.group(2))
                        titles[t_idx].score = max(titles[t_idx].score, channels * 10)
                    elif "Surround" in val or "Multichannel" in val:
                        titles[t_idx].score = max(titles[t_idx].score, 50)
                    elif "Stereo" in val or "2.0" in val:
                        titles[t_idx].score = max(titles[t_idx].score, 20)

            # ATMOS Priorities
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

    for t_idx, info in sorted(titles.items(), key=lambda x: int(x[0])):
        logger.emit(f"    [Title {t_idx}] {info.file_name}")
        logger.emit(f"      - Duration: {info.duration} ({info.chapters} Chapters)")
        logger.emit(f"      - Size: {info.size_str}")

        if info.streams:
            logger.emit("      - Streams:")
            # Sort by absolute stream_idx so it prints in order
            for s_idx, stream in sorted(info.streams.items()):
                marker = "★" if s_idx in info.atmos_streams else "->"
                logger.emit(f"          {marker} Stream {s_idx}: {stream}")
        else:
            logger.emit("      - Streams: None detected")
        logger.emit("")


def get_best_mb_candidate(target_artist: str, target_album: str, chapter_count: int,
                          target_duration_sec: float, candidates: list[dict]) -> dict | None:
    """
    Finds the best MusicBrainz candidate for a given artist, name, target chapter count, and target duration.
    Filters for exact track count matches or +1 preamble matches, prioritizing similar durations, lower MB medium index,
    and finally string similarity to the target artist and album names.
    """
    if not candidates:
        return None

    # Filter for valid matches (exact or +1 preamble)
    matched = [c for c in candidates if 0 <= (chapter_count - len(c['tracks'])) <= 1]
    if not matched:
        return None

    safe_target = normalize_for_fuzzy_comparison(f"{target_artist} {target_album}").replace(" ", "")

    def get_similarity(c: dict) -> float:
        """Calculates a similarity score based on string similarity to the target artist and album."""
        safe_cand = normalize_for_fuzzy_comparison(f"{c['artist']} {c['title']}").replace(" ", "")
        return difflib.SequenceMatcher(None, safe_target, safe_cand).ratio()

    def get_duration_penalty(c: dict) -> int:
        """Calculates a penalty based on total duration difference (in 120-second buckets)."""
        mb_duration_ms = sum(t.get('duration') or 0 for t in c['tracks'])
        if mb_duration_ms == 0 or target_duration_sec == 0:
            return 0
        mb_sec = mb_duration_ms / 1000.0
        return int(abs(target_duration_sec - mb_sec) // 120)

    def get_medium_penalty(c: dict) -> int:
        """Penalizes bonus discs and non-primary mediums."""
        # Medium 1 gets 0 penalty. Medium 2 gets 1 penalty, etc.
        return max(0, c.get('medium_index', 1) - 1)

    # Sort Hierarchy:
    # 1. Duration Penalty (Filters out radically different tracklists)
    # 2. Medium Position (Prioritizes Disc 1 over Disc 2/3/4)
    # 3. Text Similarity (Fuzzy string match for exact album/artist text)
    matched.sort(key=lambda c: (
        get_duration_penalty(c),
        get_medium_penalty(c),
        -get_similarity(c)
    ))

    for i, c in enumerate(matched):
        bullet = "*" if i == 0 else "-"
        logger.emit(f"    {bullet} {c['title']} ({c.get('year', 'Unknown')}) [MBID {c.get('mbid', 'Unknown')} "
                    f"({c.get('medium_index', 1)})] [Scores: Duration={get_duration_penalty(c)},"
                    f" Medium={get_medium_penalty(c)}, Artist/Title={get_similarity(c) :.2f}]")

    return matched[0]


def find_primary_title(source_spec: str, artist: str, album: str, prefer_legacy: bool = False)\
        -> tuple[str, dict[str, Any] | None, set[int]]:
    """Identifies the main audio title by intersecting MakeMKV and MusicBrainz metadata.

    Fetches the MusicBrainz candidates concurrently while MakeMKV scans the disc.

    Args:
        source_spec: The physical or virtual path to the Blu-ray source.
        artist: The canonicalized album artist.
        album: The canonicalized album title.
        prefer_legacy: If True, prioritizes lossless 5.1/Quad formats over Atmos.

    Returns:
        A 3-element tuple containing:
        - title_idx: The MakeMKV index of the winning title.
        - matched_candidate: The MusicBrainz release dictionary, or None if the API failed.
        - makemkv_streams: A list of human-readable stream descriptors parsed by MakeMKV.
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
            candidates = candidates_future.result(timeout=30) or []
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            logger.emit("    [!] Warning: MusicBrainz candidate pre-fetch timed out or was cancelled.")

    # Find all input titles with the same track count as a candidate MusicBrainz releases
    if candidates:
        valid_titles = []
        for title_index, info in titles.items():
            if info.score <= 0:
                logger.emit(f"    [-] Rejected Title {title_index} (No valid audio stream detected)")
                continue

            logger.emit(f"\n[+] Finding best candidate for Title {title_index}")
            best_candidate = get_best_mb_candidate(artist, album, info.chapters, info.duration_sec, candidates)

            # Keep the title if it matched a candidate, OR if MB is entirely offline
            if best_candidate or not candidates:
                valid_titles.append((title_index, best_candidate))
    else:
        # Graceful degradation if MB is down/offline
        valid_titles = [(t_idx, None) for t_idx, info in titles.items() if info.score > 0]
    if not valid_titles:
        raise RuntimeError("No titles in the input matched the expected track counts from MusicBrainz.")

    # noinspection PyShadowingNames
    def sort_key(item: tuple[str, dict | None]) -> tuple[int, int, int, int]:
        """Sort criterion: (Format Penalty, Duration Penalty, MB Rank, -Size)"""
        t_idx, matched_candidate = item

        # 1. Format Penalty: Score titles based on user intent (Atmos vs. FLAC)
        has_atmos = False
        has_lossless_legacy = False
        for s_idx, s in titles[t_idx].streams.items():
            s_low = s.lower()
            if s_idx in titles[t_idx].atmos_streams or "truehd" in s_low:
                has_atmos = True
            elif "surround" in s_low or "4.0" in s_low or "5.1" in s_low or "quad" in s_low:
                if any(codec in s_low for codec in ["lpcm", "pcm", "dts-hd ma", "flac", "alac", "truehd"]):
                    has_lossless_legacy = True
        has_target_format: bool = has_lossless_legacy if prefer_legacy else has_atmos
        format_penalty = int(not has_target_format)

        # 2. Duration Penalty: How well does this physical title match the logical album, duration-wise?
        duration_penalty = 0
        if matched_candidate:
            mb_dur_ms = sum(t.get('duration') or 0 for t in matched_candidate['tracks'])
            title_sec = titles[t_idx].duration_sec
            if mb_dur_ms > 0 and title_sec > 0:
                duration_penalty = int(abs(title_sec - (mb_dur_ms / 1000.0)) // 120)

        # 3. Relevance: Index in the MusicBrainz search results (0 is best, 999 if MB is offline)
        mb_rank = candidates.index(matched_candidate) if matched_candidate in candidates else 999

        # 4. Size: Negated so that larger files sort first when using min()
        size = titles[t_idx].size

        return format_penalty, duration_penalty, mb_rank, -size

    logger.emit("\n[*] Finding best title match across all input titles:")
    valid_titles.sort(key=sort_key)
    for i, vt in enumerate(valid_titles):
        title_index, matched_candidate = vt
        format_penalty, duration_penalty, mb_rank, neg_size = sort_key(vt)
        bullet = "*" if i == 0 else "-"
        if matched_candidate is not None:
            mc = cast(dict, cast(object, matched_candidate))  # Re-anchors the type for PyCharm, whose typechecker is dumb
            logger.emit(f"    {bullet}  Title {title_index} [{mc['title']} ({mc.get('year', 'Unknown')})]"
                    f" [MBID {mc.get('mbid', 'None')} ({mc.get('medium_index', 1)})]"
                    f" [Scores: Format={format_penalty}, Duration={duration_penalty}, Rank={mb_rank}, Size={neg_size}]")
        else:
            logger.emit(f"    {bullet}  Title {title_index} ['Unknown']"
                        f"[Scores: Rank={mb_rank}, Format={format_penalty}, Size={-neg_size} bytes]")
    logger.emit("")

    winner = valid_titles[0]
    return winner[0], winner[1], titles[winner[0]].atmos_streams


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
                                 [r"C:\ffmpeg\bin\ffmpeg.exe", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]) or ""
        self.FFPROBE = self._find("ffprobe",
                                  [r"C:\ffmpeg\bin\ffprobe.exe", "/usr/local/bin/ffprobe", "/opt/homebrew/bin/ffprobe"]) or ""
        self.MKVMERGE = self._find("mkvmerge",
                                   [r"C:\Program Files\MKVToolNix\mkvmerge.exe", "/usr/local/bin/mkvmerge",
                                    "/opt/homebrew/bin/mkvmerge"]) or ""
        self.MAKEMKV = self._find("makemkvcon64" if self.IS_WIN else "makemkvcon", [
            r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
            "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
            "/usr/bin/makemkvcon"
        ]) or ""

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
TOOLS: Toolset = cast(Toolset, cast(Any, None)) # Whatever it takes...


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
    res = run_command(cmd, f"Ripping Title {title_idx}", suppress_summary=True)
    elapsed = time.time() - start_time

    mkv_files = list(out_path.glob("*.mkv"))
    if not mkv_files:
        # Catch MakeMKV's silent failure when the disk fills up
        if "disk full" in res.lower() or "disk was full" in res.lower() or "no space left" in res.lower():
            raise RuntimeError(
                "Extraction failed because your temporary drive ran out of disk space. Please free up space.")
        raise RuntimeError("MakeMKV produced no output.")

    winner = mkv_files[0]

    size_mb = winner.stat().st_size / (1024 * 1024)
    logger.emit(
        f"[+] Title extraction complete: {size_mb:.1f} MB in {elapsed:.1f} seconds (Avg: {size_mb / elapsed:.1f} MB/s)")
    logger.emit("")

    return winner


def find_atmos_stream(mkv_path: Path, atmos_streams: set[int], preferred_codec: str = "truehd") -> int | None:
    """
        Returns the index of the highest quality Atmos stream based on the preferred_codec,
        with appropriate fallbacks and warnings. Evaluates based on Format, Channels, and Stream Index.
    """
    logger.emit("\n[*] === AUDIO STREAM ANALYSIS ===")
    cmd = [TOOLS.FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index,channels,codec_name",
           "-of", "json", str(mkv_path)]
    res = run_command(cmd, "Scanning for Atmos Stream")

    try:
        streams: list[dict[str, Any]] = json.loads(res).get('streams', [])
        if not streams:
            return None

        valid_streams = [s for s in streams if s.get('codec_name', '').lower() in [preferred_codec, "eac3", "ac3"]]
        if not valid_streams:
            return None

        def stream_sort_key(s):
            """Sort criterion: (Format Penalty, -Channels, Index)"""
            idx = int(s.get('index', 999))
            codec = s.get('codec_name', '').lower()

            if codec == preferred_codec:
                # STRICT check: Did MakeMKV explicitly flag this absolute stream index as Atmos?
                if atmos_streams:
                    fmt_penalty = 0 if idx in atmos_streams else 1
                else:
                    # Failsafe if running against a raw MKV file without MakeMKV data
                    fmt_penalty = 0 if int(s.get('channels', 0)) >= 8 else 1
            elif codec == "eac3":
                fmt_penalty = 2
            elif codec == "ac3":
                fmt_penalty = 3
            else:
                fmt_penalty = 4

            channels = int(s.get('channels', 0))
            return fmt_penalty, -channels, idx

        valid_streams.sort(key=stream_sort_key)

        logger.emit(f"[*] Evaluating {len(valid_streams)} valid Atmos candidate streams...")
        for i, s in enumerate(valid_streams):
            fmt_pen, neg_chan, s_idx = stream_sort_key(s)
            bullet = "*" if i == 0 else "-"
            logger.emit(f"    {bullet} Index {s_idx} [Codec: {s.get('codec_name', 'unknown')}, Channels: {-neg_chan}] "
                        f"[Scores: Format={fmt_pen}, Channels={neg_chan}, Index={s_idx}]")

        best_stream = valid_streams[0]
        best_idx = int(best_stream['index'])
        best_codec = best_stream.get('codec_name', 'unknown')
        best_channels = best_stream.get('channels', 'unknown')

        best_fmt_pen = stream_sort_key(best_stream)[0]
        if best_fmt_pen == 1:
            logger.emit("    [!] WARNING: No TrueHD Atmos found! Falling back to standard TrueHD/Lossy!")
        elif best_fmt_pen == 2:
            logger.emit("    [!] WARNING: NO ATMOS METADATA DETECTED! Falling back to lossy EAC3-JOC!")

        logger.emit(f"\n[+] Selected Primary Audio Stream: Index {best_idx} ({best_codec}, {best_channels} channels)")
        return best_idx

    except json.JSONDecodeError:
        return None


def find_multichannel_stream(mkv_path: Path) -> tuple[int, int] | None:
    """
    Returns a tuple of (stream_index, channel_count) for the best lossless multichannel stream
    (e.g., LPCM, DTS-HD MA, TrueHD), ignoring lossy streams.
    Prioritizes dedicated legacy mixes over Atmos, and breaks ties via stream index.
    """
    logger.emit("\n[*] === LOSSLESS MULTICHANNEL STREAM ANALYSIS ===")

    cmd = [TOOLS.FFPROBE, "-v", "error", "-select_streams", "a",
           "-show_entries", "stream=index,channels,codec_name,profile",
           "-of", "json", str(mkv_path)]

    res = run_command(cmd, "Scanning for Lossless Multichannel Stream")
    try:
        streams = json.loads(res).get('streams', [])
        lossless_codecs = {'truehd', 'pcm_s16le', 'pcm_s24le', 'pcm_s24be', 'mlp', 'alac', 'flac'}
        valid_streams = []

        for s in streams:
            codec = s.get('codec_name', '').lower()
            profile = s.get('profile', '').lower()

            is_lossless = (codec in lossless_codecs) or (
                        codec == 'dts' and ('master audio' in profile or 'ma' in profile))
            if is_lossless:
                valid_streams.append(s)

        if not valid_streams:
            logger.emit("    [!] ERROR: No valid lossless multichannel streams found on disc!")
            return None

        # noinspection shadowing-names
        def mc_sort_key(s):
            """Sort criterion: (Atmos Penalty, -IsMultichannel, -Channels, Index)"""
            profile = s.get('profile', '').lower()
            channels = int(s.get('channels', 0))
            idx = int(s.get('index', 999))

            is_multichannel = 1 if channels >= 4 else 0
            is_atmos = 1 if "atmos" in profile else 0

            return is_atmos, -is_multichannel, -channels, idx

        valid_streams.sort(key=mc_sort_key)

        logger.emit(f"[*] Evaluating {len(valid_streams)} valid lossless candidate streams...")
        for i, s in enumerate(valid_streams):
            is_atmos, neg_multi, neg_chan, s_idx = mc_sort_key(s)
            bullet = "*" if i == 0 else "-"
            logger.emit(f"    {bullet} Index {s_idx} [Codec: {s.get('codec_name', 'unknown')}, Channels: {-neg_chan}] "
                        f"[Scores: AtmosPen={is_atmos}, Multi={-neg_multi}, Channels={neg_chan}, Index={s_idx}]")

        best_stream = valid_streams[0]
        best_idx = int(best_stream['index'])
        best_channels = int(best_stream.get('channels', 0))

        logger.emit(
            f"\n[+] Selected Lossless Stream: Index {best_idx} ({best_stream.get('codec_name')}, {best_channels} channels)")

        return best_idx, best_channels
    except json.JSONDecodeError:
        return None


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
    All the releases returned will come from the same release group.
    """
    logger.emit("\n[*] === STARTING METADATA FETCH ===")

    rg = find_release_group(album, artist)
    if not rg:
        logger.emit("    [-] No matching release group found. Aborting metadata fetch.")
        return []
    rg_id, rg_artist, rg_title = rg

    releases = find_releases_and_dates_for_release_group(rg_id, rg_title)
    logger.emit(f"    -> Filtered down to {len(releases)} matching release(s).")

    if not releases:
        logger.emit("    [-] No matching releases found. Aborting metadata fetch.")
        return []

    candidates = fetch_tracklists_for_releases(releases, rg_id, rg_artist, rg_title)
    return candidates


def find_release_group(album: str, artist: str) -> tuple[str, str, str] | None:
    """
    Finds the release group corresponding to the given album and artist name (which may be inexact).
    Searches by release rather than release group to bypass strict artist indexing.

    Returns:
        A tuple containing (release_group_id, artist, title) if found, otherwise None.
    """
    for is_strict in [True, False]:
        query = f'artist:"{artist}" AND release:"{album}"' if is_strict else f'"{artist}" "{album}"'
        logger.emit(f"[*] Executing Query: {query}")
        try:
            res = _search_releases(query=query, limit=MAX_RELEASES_TO_SEARCH)
            for r in res.get('release-list', []):
                found_artist = extract_artist_from_musicbrainz_metadata(r)
                found_album = r.get('title', 'Unknown')

                # Evaluate matches for plausibility with "substring leniency"
                artist_match = _is_safe_match(artist, found_artist)
                album_match = _is_safe_match(album, found_album)

                if artist_match and album_match:
                    rg_id = r.get('release-group', {}).get('id')
                    rg_title, rg_artist = found_album, found_artist
                    logger.emit(f"    [+] Match Found: {found_artist} - {found_album} (RG ID: {rg_id})\n")

                    # Ensure rg_id actually exists to satisfy the strict return type!
                    if rg_id:
                        return str(rg_id), rg_artist, rg_title
                else:
                    logger.emit(f"    [-] Rejected Candidate: {found_artist} - {found_album} (Artist Match: {artist_match}, Album Match: {album_match})")
        except mb.WebServiceError as e:
            logger.emit(f"    [!] API Error: {e}")

    return None


@retry_mb_api()
def _search_releases(query: str, limit: int) -> dict:
    """Executes a release search with retry logic."""
    return mb.search_releases(query=query, limit=limit)


def find_releases_and_dates_for_release_group(rg_id: str, rg_title: str) -> list[tuple[str, str]]:
    """
    Returns release IDs and dates of releases of the given release group corresponding to all possible track-counts.
    Evaluates mediums individually to strictly match physical disc topology.
    """
    logger.emit(f"[*] Fetching all releases and mediums for Release Group: {rg_id} ({rg_title})")

    releases = []
    limit = 100
    offset = 0

    # 1. Fetch all releases by paginating through the browse_releases endpoint
    try:
        while True:
            result = _browse_releases(rg_id, limit, offset)
            batch = result.get('release-list', [])
            releases.extend(batch)

            if len(batch) < limit:
                break  # We've reached the end of the list
            offset += limit

        logger.emit(f"    -> API returned {len(releases)} releases in this group.")
    except mb.WebServiceError as e:
        logger.emit(f"    [!] Error fetching releases: {e}")
        return []

    unique_releases = {}
    seen_counts = set()

    # 2. Map the mediums to find unique track counts
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

    logger.emit(f"    -> Identified unique track counts: {sorted(list(seen_counts))}")
    return [(r_id, date) for r_id, date in unique_releases.items()]

@retry_mb_api()
def _browse_releases(rg_id: str, limit: int, offset: int) -> dict:
    """Fetches a paginated list of releases with retry logic."""
    return mb.browse_releases(release_group=rg_id, includes=['media'], limit=limit, offset=offset)


def fetch_tracklists_for_releases(release_ids_and_dates: list[tuple[str, str]],
                                  rg_id: str, rg_artist: str, rg_title: str) -> list[dict[str, Any]]:
    """Fetch the tracklists for the given release ids (and dates), which pertain to the given release group metadata"""
    logger.emit(f"\n[*] Fetching tracklists for {len(release_ids_and_dates)} matching MB releases in release group {rg_id}...")
    candidates = []
    for rel_id, year in release_ids_and_dates:
        try:
            rel_info = _fetch_release_info(rel_id)

            # Treat EVERY medium as its own independent candidate
            for pos, medium in enumerate(rel_info.get('release', {}).get('medium-list', []), start=1):
                medium_tracks = []
                for track in medium.get('track-list', []):
                    medium_tracks.append({
                        'title': sanitize_track_title(track.get('recording', {}).get('title', 'Unknown Track')),
                        'duration': int(track.get('recording', {}).get('length') or 0)
                    })

                if medium_tracks:
                    candidates.append({
                        'title': rg_title,
                        'artist': rg_artist,
                        'year': year or 'Unknown',
                        'mbid': rel_id,  # Specific release MBID instead of the Release Group ID
                        'medium_index': int(medium.get('position', pos)),
                        'tracks': medium_tracks
                    })
        except mb.WebServiceError:
            continue
    logger.emit(f"[*] Metadata fetch complete. Found {len(candidates)} candidate MB mediums in release group.")
    return candidates


def sanitize_track_title(title: str) -> str:
    """
    Strips Extra Title Information (MusicBrainz ETI) from track titles safely.
    Removes parenthetical blocks ONLY if they contain known technical/mix keywords.
    Preserves structural parentheticals like "(Don't Fear) The Reaper".
    """
    # A blacklist of words that strongly indicate technical metadata rather than a song title
    eti_keywords = r'(mix|remix|remaster|master|version|edit|live|instrumental|demo|take|stereo|mono|surround|atmos|acoustic)'

    # Matches " (" or "(", followed by anything, the keyword (as a whole word), anything, and ")"
    pattern = re.compile(rf'\s*\([^)]*\b{eti_keywords}\b[^)]*\)', re.IGNORECASE)

    # Strip the ETI and clean up any lingering trailing spaces
    return pattern.sub('', title).strip()


@retry_mb_api()
def _fetch_release_info(rel_id: str) -> dict:
    """Fetches a single release with retry logic."""
    return mb.get_release_by_id(rel_id, includes=['recordings'])

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
    Acts as a lenient gatekeeper: allows pure substrings (for truncated titles or
    collaborations) or highly similar strings (for typos/acronyms).
    """
    # Universal substring safety valve for messy artist collaborations or truncated titles
    # (e.g., "Scary Monsters" in "Scary Monsters (and Super Creeps)")
    if expected.lower() in found.lower():
        return True

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
    """ Deletes the given directory with extreme prejudice, even if other processes have it locked. """
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
def _signal_handler(_signum: object, _frame: object) -> NoReturn:
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


def get_mkv_master_file_and_metadata(src_path: str, artist: str, album: str, output_container: str)\
        -> tuple[Path, dict[str, Any] | None, list[dict[str, Any]], float, set[int]]:
    """Acquires the master MKV file, extracts its chapters and duration, and fetches the matching MusicBrainz metadata.

    Handles polymorphic source inputs (optical disc indices, ISOs, BDMV folders, standalone MKVs,
    or IAA folders), ripping or merging them as necessary into a single master MKV in the temporary workspace.

    Args:
        src_path: The physical or virtual path to the source material.
        artist: The requested album artist, used for metadata querying.
        album: The requested album title, used for metadata querying.
        output_container: The target output file extension (e.g., ".m4a", ".flac"), used to determine
            if a legacy lossless rip is explicitly requested.

    Returns:
        A 5-element tuple containing:
        - master_mkv: The Path to the consolidated MKV file that is ready for audio extraction.
        - matched_candidate: The matched MusicBrainz release dictionary, or None if the API failed or no match was found.
        - chapters: A list of chapter dictionaries extracted from the master MKV via ffprobe.
        - duration: The total duration of the master MKV in seconds.
        - atmos_streams: A ints which are the indices of the streams containing Atmos audio
          (will be empty if the source bypassed MakeMKV).
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
    atmos_streams: set[int] = set()
    if source_spec:
        title_idx, matched_candidate, atmos_streams = find_primary_title(source_spec, artist, album, output_container == ".flac")
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
        matched_candidate = get_best_mb_candidate(artist, album, len(chapters), duration, candidates)

        # Fallback: if no strict match was found but we HAVE candidates, just blindly trust the top result
        if not matched_candidate and candidates:
            matched_candidate = candidates[0]

    return atmos_mkv, matched_candidate, chapters, duration, atmos_streams


def _tag_flac_files(flac_files: list[tuple[Path, dict, int]], album: str, album_artist: str, year: str, cover_path: Path):
    """Injects metadata and cover art into the sliced FLAC files."""
    logger.emit("[*] Applying metadata and embedded artwork to FLAC files...")

    # Preload cover art if it exists
    pic = None
    if cover_path.exists():
        pic = Picture()
        with open(cover_path, "rb") as f:
            pic.data = f.read()
        pic.type = 3  # Front Cover
        pic.mime = "image/jpeg"
        pic.desc = "Front Cover"

    for filepath, track_data, track_num in flac_files:
        audio = FLAC(filepath)

        # Clear existing tags just in case FFmpeg pulled garbage from the MKV
        audio.delete()

        # Inject standard tags
        audio['title'] = track_data['title']
        audio['artist'] = track_data.get('artist', album_artist)
        audio['albumartist'] = album_artist
        audio['album'] = album
        audio['date'] = str(year)
        audio['tracknumber'] = str(track_num)
        audio['totaltracks'] = str(len(flac_files))
        if pic:
            audio.add_picture(pic)

        audio.save()



def _log_prologue(album: str, artist: str, output_container: str, preferred_codec: str, src_path: str):
    """Emit rip prologue to logger (to enhance readability of log file in isolation)"""
    logger.emit("=== Carat Rip Log ===")
    start_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
    logger.emit(f"Date/Time : {start_time_str}")
    logger.emit(f"Source    : {src_path}")
    logger.emit(f"Artist    : {artist}")
    logger.emit(f"Album     : {album}")
    logger.emit(f"Format    : {output_container.upper()} (Codec: {preferred_codec})")
    logger.emit("=====================\n")


class AudioProfile(NamedTuple):
    """
    The resolved audio stream and destination formatting for a ripping job.

    Attributes:
        idx (int): The FFmpeg stream index of the selected audio track in the master file.
        container (str): The target file extension for the output (e.g., '.m4a', '.flac').
        suffix (str): The descriptive tag appended to the final filename (e.g., '(Atmos)', '(Surround)').
    """
    idx: int
    container: str
    suffix: str


def resolve_audio_profile(master_mkv: Path, atmos_streams: set[int], requested_container: str, preferred_codec: str) -> AudioProfile:
    """
    Scans the master MKV and determines the optimal audio stream and output format.
    Will automatically fall back to lossless FLAC slicing if Atmos is requested but unavailable.
    """

    # 1. Attempt Atmos (if permitted by the user)
    if requested_container != ".flac":
        idx = find_atmos_stream(master_mkv, atmos_streams, preferred_codec)
        if idx is not None:
            return AudioProfile(idx=idx, container=requested_container, suffix="(Atmos)")

        logger.emit("\n[!] No Dolby Atmos stream found on source.")
        logger.emit("[*] Automatically switching to FLAC slicing for legacy lossless surround...")

    # 2. Fallback to Legacy Multichannel (or proceed if FLAC was explicitly requested)
    legacy_info = find_multichannel_stream(master_mkv)
    if legacy_info is None:
        raise ValueError("No compatible audio stream (LPCM, DTS-HD MA, TrueHD, MLP, alac, flac) found in master file.")

    idx, channels = legacy_info

    if channels == 4:
        suffix = "(Quad)"
    elif channels <= 2:
        suffix = "(Stereo)"
    else:
        suffix = "(Surround)"

    return AudioProfile(idx=idx, container=".flac", suffix=suffix)


@dataclass
class RipContext:
    """Encapsulates all state and metadata required for the final AV assembly phase."""
    master_mkv: Path  # The source MKV file containing the audio stream
    dest: Path  # The destination directory for the final audio files
    artist: str  # The canonicalized artist name
    album: str  # The canonicalized album title
    clean_album: str  # The sanitized album title used for file naming
    profile: AudioProfile # The resolved audio stream index and destination formatting
    duration: float  # The total duration of the source in seconds
    info: dict[str, Any]  # The matched MusicBrainz release metadata
    chapters: list[dict]  # The extracted chapter markers
    tracks: list[dict]  # The tracklist metadata from MusicBrainz


def _assemble_gapless_album(ctx: RipContext) -> None:
    """Handles the FFmpeg remuxing for gapless M4A or MKV containers."""
    final_audio_name = f"{ctx.clean_album} {ctx.profile.suffix}{ctx.profile.container}"

    if ctx.tracks:
        ctx.chapters = ctx.chapters[:len(ctx.tracks)]

    generate_cue_sheet(ctx.dest / f"{ctx.clean_album} {ctx.profile.suffix}.cue", final_audio_name, ctx.info,
                       ctx.chapters, ctx.tracks)

    cmd = [
        TOOLS.FFMPEG, "-hide_banner", "-loglevel", "error", "-stats",
        "-probesize", "100M", "-analyzeduration", "100M",
        "-i", str(ctx.master_mkv), "-map", f"0:{ctx.profile.idx}",
        "-metadata", f"title={ctx.album}", "-c:a", "copy"
    ]

    mbid = ctx.info.get('mbid', "unknown")
    if mbid:
        cmd.extend(["-metadata", f"MusicBrainz_Album_Id={mbid}"])

    if ctx.profile.container == ".m4a":
        cmd.extend(["-f", "mp4", "-movflags", "+faststart", "-strict", "-2"])
    else:
        cmd.extend(["-f", "matroska"])

    cmd.extend(["-fflags", "+genpts", "-map_chapters", "-1", "-y", str(ctx.dest / final_audio_name)])

    run_command(cmd, f"Finalizing {ctx.profile.suffix[1:-1]} {ctx.profile.container[1:].upper()}",
                {"ffmpeg_duration": ctx.duration})

def _extract_flac_tracks(ctx: RipContext) -> list[tuple[Path, dict, int]]:
    """Handles slicing the master MKV into individual FLAC tracks."""
    logger.emit(f"[*] Slicing tracks to FLAC {ctx.profile.suffix}...")
    if ctx.profile.idx is None:
        raise ValueError("No compatible audio stream (LPCM, DTS-HD MA, TrueHD, MLP, alac, flac) found in master file.")

    flac_files = []
    for i, (track, chapter) in enumerate(zip(ctx.tracks, ctx.chapters)):
        track_num = i + 1
        safe_title = _sanitize_filename(track['title'])
        out_filename = f"{track_num:02d} {safe_title}.flac"
        out_path = ctx.dest / out_filename
        flac_files.append((out_path, track, track_num))

        start_time = float(chapter['start_time'])
        end_time = float(chapter['end_time'])

        cmd = [
            TOOLS.FFMPEG, '-hide_banner', '-loglevel', 'error', '-stats', '-y',
            '-ss', str(start_time), '-to', str(end_time),
            '-i', str(ctx.master_mkv),
            '-map', f"0:{ctx.profile.idx}",
            '-c:a', 'flac',
            str(out_path)
        ]

        env = {
            "ffmpeg_duration": ctx.duration,
            "ffmpeg_time_offset": start_time,
            "ffmpeg_prefix": "Slicing"
        }
        run_command(cmd, f"Extracting track {track_num} from MKV master ({track['title']})", env)

    return flac_files


class Container(StrEnum):
    """
    Container formats supported by Carat.

    Attributes:
        M4A: The standard MPEG-4 audio container (typically used for Atmos/MP4).
        MKV: The Matroska multimedia container (typically used for pure lossless extraction).
        FLAC: The Free Lossless Audio Codec container (used only for legacy, non-Atmos recordings).
    """
    M4A = ".m4a"
    MKV = ".mkv"
    FLAC = ".flac"


class Codec(StrEnum):
    """
    Preferred audio codecs for extraction, ordered by descending quality.

    Attributes:
        TRUEHD: Dolby TrueHD. The highest quality, lossless codec.
                This is the preferred format for pure Atmos extraction.
        EAC3: Dolby Digital Plus (E-AC-3-JOC). A high-quality but lossy codec
              that can carry Atmos metadata. Used as a primary fallback.
        AC3: Dolby Digital (AC-3). A legacy, lossy surround codec (typically 5.1).
             Critically, this codec does NOT support Dolby Atmos.
        FLAC: The Free Lossless Audio Codec. The only valid encoding for Container.flac.
    """
    TRUEHD = "truehd"
    EAC3 = "eac3"
    AC3 = "ac3"
    FLAC = "flac"


def rip_album_to_library(src_path: str, artist: str, album: str, library_root: str,
                         output_container: Container = Container.M4A, preferred_codec: Codec = Codec.TRUEHD) -> bool:
    """
    Rips the specified Atmos or multichannel source into a digital music library.

    The artist and album title are used to obtain metadata and cover art, which are used to
    generate the cue file and cover.jpg in the music library. The library entry generally consists
    of a chapterless audio file (M4A or MKV) containing only the Atmos stream, a cue sheet, and
    a cover.jpg. This format provides gapless playback of the entire album, as well as access to
    individual tracks, and is the only format known to do so on most platforms.

    If no metadata can be found (e.g., if MusicBrainz has no entry for the release, there is no
    internet connection, or MusicBrainz is down), the album will still be ripped, but the track titles
    will all be "Track <#>" and the artist and album names will not be canonicalized.

    The output codec will be the highest quality codec consistent with the caller's preferences. The
    three possibilities, in order of decreasing quality, are TrueHD Atmos (lossless), E-AC-3-JOC Atmos
    (lossy), and AC-3 Surround (not Atmos!).

    If output_container is ".flac", or it's another value but the input does not contain an Atmos stream,
    carat will find the "best" lossless stream and rip it into a collection of flac files (with tags and
    no cue sheet). By "best," we mean the stream with the most channels or if there is a tie, the one
    with the most bytes (total length).

    Polymorphic Input Handling:
      - Integers (e.g. "0", "-1") are Treated as Physical Optical Disc indices.
      - .iso files are Mounted virtually and scanned as discs.
      - BDMV folders are Scanned as Blu-ray structures.
      - .mkv files are Treated as direct sources, bypassing MakeMKV rip (Headphone Dust release format).
      - Standard folders are treated as collections of tracks to be merged into an album (IAA release format).

    The processing pipeline ensures:
      1. A temporary workspace is used for intermediate files.
      2. Metadata is fetched from MusicBrainz only if the track count matches what's found on the input source.
      3. CUE sheets are generated and internal chapters are stripped for gapless playback support.
      4. Time-consuming tasks (e.g., Cover Art download, Remuxing) are parallelized where possible.

    Args:
        src_path: The polymorphic source path (Disc index, ISO, BDMV folder, MKV, or IAA folder).
        artist: The requested album artist (used for metadata fetching).
        album: The requested album title (used for metadata fetching).
        library_root: The destination directory for the processed album.
        output_container: The target container format (.m4a, .mkv, or .flac).
        preferred_codec: The target Atmos codec (truehd, eac3, or ac3).

    Returns:
        bool: True if tracklist metadata was successfully found and applied, False otherwise.
    """

    ingestion_start_time = time.time()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    lib_path = Path(library_root)
    _ensure_writable(lib_path)
    logger.open_log_file(TMP_DIR / "carat_rip.log") # We log to tmp dir because we don't yet know the final destination
    _log_prologue(album, artist, output_container, preferred_codec, src_path)

    # This log_dest value is tentative. It will be used only if the rip fails early on; otherwise it will be overwritten
    clean_artist = _sanitize_filename(artist)
    clean_album = _sanitize_filename(album)
    log_name = f"{clean_artist} - {clean_album}.log".replace("  ", " ").strip()
    log_dest = lib_path / clean_artist / clean_album / log_name

    try:
        # Extract master mkv and metadata from input file (or disc) and web metadata resources
        master_mkv, matched_candidate, chapters, duration, atmos_streams = \
            get_mkv_master_file_and_metadata(src_path, artist, album, output_container)

        # Canonicalize artist and album title
        if matched_candidate:
            canonicalized_artist = matched_candidate.get('artist', artist)
            canonicalized_album = matched_candidate.get('title', album)
            if (artist != canonicalized_artist) or (album != canonicalized_album):
                artist = canonicalized_artist
                album = canonicalized_album
                logger.emit(f"[+] Canonicalized as Artist: {artist}, Album: {album}")
            info = matched_candidate
        else:
            logger.emit(f"[!] No MusicBrainz metadata for Artist: {artist}, Album: {album}")
            info = {'artist': artist, 'title': album, 'year': 'Unknown'}

        # Sanitize artist and album title
        clean_artist = _sanitize_filename(artist)
        clean_album = _sanitize_filename(album)
        if (clean_artist != artist) or (clean_album != album):
            logger.emit(f"[+] Sanitized as Artist: {clean_artist}, Album: {clean_album}")

        # Perform pre-assembly tasks
        profile = resolve_audio_profile(master_mkv, atmos_streams, output_container, preferred_codec)
        dest = lib_path / clean_artist / f"{clean_album} {profile.suffix}"
        log_dest = dest / f"{clean_artist} - {clean_album} {profile.suffix}.log"
        dest.mkdir(parents=True, exist_ok=True)

        # Build the immutable state context
        ctx = RipContext(
            master_mkv=master_mkv, dest=dest, artist=artist, album=album,
            clean_album=clean_album, profile=profile, duration=duration,
            info=info, chapters=chapters, tracks=(info.get('tracks', []))
        )

        # Execute Assembly (Concurrent)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            cover_future = ex.submit(get_cover_art.download_cover_art, artist, album, dest, info.get('mbid'))

            if profile.container != ".flac":
                _assemble_gapless_album(ctx)
            else:
                flac_files = _extract_flac_tracks(ctx)

            try:
                cover_future.result(timeout=45)
            except (concurrent.futures.TimeoutError, Exception):
                pass

            if profile.container == ".flac":
                _tag_flac_files(flac_files, album, artist, info.get('year', 'Unknown'), dest / "cover.jpg")

        logger.emit(f"\n[+] Ingestion Complete: {album}")
        logger.emit(f"[+] Total elapsed time: {(time.time() - ingestion_start_time):.1f} seconds.")
    finally:
        logger.close_log_file(log_dest)
        clean_up()

    return bool(matched_candidate)


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
    parser.add_argument("--output-container", choices=["m4a", "mkv", "flac"], default="m4a",
                        help="Output container format (default: m4a)")
    parser.add_argument("--preferred-codec", choices=["truehd", "eac3", "ac3"], default="truehd",
                        help="Preferred audio codec (default: truehd)")

    args = parser.parse_args()

    # Initialize for CLI (no UI handler)
    init()

    got_metadata = rip_album_to_library(
        _clean_path_arg(args.source),
        args.artist,
        args.album,
        _clean_path_arg(args.library_root),
        output_container=f".{args.output_container}",
        preferred_codec=args.preferred_codec
    )

    if not got_metadata:
        logger.emit("\n[!] Could not find metadata for this release. Please check artist and album names and internet connection.")
        sys.exit(1)

if __name__ == "__main__":
    main()
