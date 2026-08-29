"""
Carat - Concise Atmos Ripping Automation Tool.

A GUI tool, command line tool, and library for ripping Dolby Atmos albums (digital and physical) into digital music
libraries, providing gapless playback and track selection. This tool emphasizes ease of use over flexibility. With a
single click, carat automatically gets metadata and cover art from trusted sources (MusicBrainz, CAA, and Apple), and
supports all popular Atmos distribution formats (Blu-ray, mkv, mp4, BDMV).
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

from __future__ import annotations

__version__ = "1.0B4.1"

__all__ = ['rip_album_to_library', 'Container', 'Codec', 'prepare_album_rip', 'execute_album_rip', 'RipPlan']

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
from collections import defaultdict
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, NoReturn, NamedTuple, cast, TypedDict

# noinspection PyProtectedMember
from mutagen.flac import FLAC, Picture

import get_cover_art
import logger
import makemkv_updater
from music_brainier import MusicBrainzMetadata, MusicBrainzTrack, get_best_mb_candidate, fetch_candidate_metadata, \
    fetch_metadata
from util import _clean_path_arg


# --- (1) Metadata & Utils ---

class MkvChapter(TypedDict):
    """Represents a physical chapter boundary parsed from the MKV file."""
    id: int
    start_time: float
    end_time: float
    title: str

def seconds_to_cue(seconds: float) -> str:
    """Converts seconds to MM:SS:FF for gapless CUE sheets."""
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}:{int((seconds % 1) * 75):02d}"


def generate_cue_sheet(cue_path: Path, audio_file_name: str, info: MusicBrainzMetadata, chapters: list[MkvChapter],
                       mb_tracks: list[MusicBrainzTrack]) -> None:
    """Generates CUE sheet for track indexing into gapless playback."""
    with cue_path.open('w', encoding='utf-8') as f:
        f.write(f'PERFORMER "{info["artist"]}"\nTITLE "{info["title"]} (Atmos)"\n'
                f'REM DATE {info.get("year", "Unknown")}\nFILE "{audio_file_name}" WAVE\n')
        for i, ch in enumerate(chapters):
            title = mb_tracks[i]['title'] if i < len(mb_tracks) else f"Track {i + 1}"
            f.write(f'  TRACK {i + 1:02d} AUDIO\n    TITLE "{title}"\n    INDEX 01 {seconds_to_cue(ch["start_time"])}\n')


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
    num_chapters: int = 0
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
                if attr_id == "8": titles[t_idx].num_chapters = int(val)
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
        logger.emit(f"      - Duration: {info.duration} ({info.num_chapters} Chapters)")
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


def find_primary_title(source_spec: str, artist: str, album: str, prefer_legacy: bool = False)\
        -> tuple[str, MusicBrainzMetadata | None, set[int], dict[str, TitleInfo]]:
    """Identifies the main audio title by intersecting MakeMKV and MusicBrainz metadata.

    Fetches the MusicBrainz candidates concurrently while MakeMKV scans the disc.

    Args:
        source_spec: The physical or virtual path to the Blu-ray source.
        artist: The canonicalized album artist.
        album: The canonicalized album title.
        prefer_legacy: If True, prioritizes lossless 5.1/Quad formats over Atmos.

    Returns:
        A 4-element tuple containing:
        - title_idx: The MakeMKV index of the winning title.
        - matched_candidate: The MusicBrainz release dictionary, or None if the API failed.
        - atmos_streams: A set of stream indices for all streams containing Atmos audio.
        - disc_topology: A dictionary containing the disc topology parsed from MakeMKV.
    """

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as prefetch_ex:
        # 1. Fire off the network fetch in the background
        MUSICBRAINZ_TIMEOUT = 60.0  # Maximum time (s) to wait for MusicBrainz
        fetch_start = time.time()
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
        elapsed = time.time() - fetch_start
        remaining_timeout = max(0.1, MUSICBRAINZ_TIMEOUT - elapsed)
        try:
            if not candidates_future.done():
                logger.emit(f"[*] Waiting for MusicBrainz metadata (Timeout in {remaining_timeout:.0f}s)...")
            candidates = candidates_future.result(timeout=remaining_timeout) or []
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
            best_candidate = get_best_mb_candidate(artist, album, info.num_chapters, info.duration_sec, candidates)

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
            mb_dur_ms = sum(t.get('duration_ms') or 0 for t in matched_candidate['tracks'])
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
    return winner[0], winner[1], titles[winner[0]].atmos_streams, titles


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

    import music_brainier # Local import to avoid circular dependencies
    music_brainier.init(__version__)


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


def extract_chapters_and_duration_from_mkv(mkv_path: Path) -> tuple[list[MkvChapter], float]:
    """Returns a list of chapters and the total duration in seconds from the given mkv file."""
    cmd = [TOOLS.FFPROBE, "-v", "quiet", "-print_format", "json", "-show_chapters", "-show_format", str(mkv_path)]
    res = run_command(cmd, "Extracting Chapter Markers")
    try:
        data = json.loads(res)
        duration = float(data.get('format', {}).get('duration', 0))

        chapters: list[MkvChapter] = []
        for ch in data.get('chapters', []):
            chapters.append({
                'id': int(ch.get('id', 0)),
                'start_time': float(ch.get('start_time', 0.0)),
                'end_time': float(ch.get('end_time', 0.0)),
                'title': ch.get('tags', {}).get('title', 'Unknown')
            })

        return chapters, duration
    except (json.JSONDecodeError, ValueError) as e:
        logger.emit(f"[!] ERROR: Failed to parse ffprobe chapter data: {e}")
        res_snippet = res[:500] + ("..." if len(res) > 500 else "")
        logger.emit(f"[!] ffprobe raw output snippet: {res_snippet}")
        return [], 0.0


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


@dataclass(frozen=True)
class AcquisitionResult:
    """ The data obtained by the (joint) metadata lookup and physical extraction phase of the rip """
    master_mkv_path: Path # The Path to the consolidated (all tracks and streams) MKV file that is ready for audio extraction
    matched_candidate: MusicBrainzMetadata | None  # The MusicBrainz metadata for the chosen release/medium, or None if
                                                    # the API failed or no match was found.
    chapters: list[MkvChapter] # A list of chapter dictionaries extracted from the master MKV (incl. track timings)
    duration: float # The actual duration of the master MKV in seconds (the total of all tracks)
    atmos_streams: set[int] # The indices of the streams in the master MKV containing Atmos audio
    disc_topology: dict[str, TitleInfo] # The disc topology parsed from MakeMKV.
    title_idx: str # The index of the matched title in the Blu-ray's title table (hence disc_topology)
    mkv_src_spec: str | None  # The formatted MakeMKV target (e.g., "disc:0"), or None if MakeMKV was bypassed


def get_mkv_master_file_and_metadata(src_path: str, artist: str, album: str, output_container: str) -> AcquisitionResult:
    """Acquires the master MKV file, extracts its chapter timings and duration, and fetches its MusicBrainz metadata.

    Handles polymorphic source inputs (optical disc indices, ISOs, BDMV folders, standalone MKVs,
    or IAA folders), ripping or merging them as necessary into a single master MKV in the temporary workspace.

    Args:
        src_path: The physical or virtual path to the source material.
        artist: The requested album artist, used for metadata querying.
        album: The requested album title, used for metadata querying.
        output_container: The target output file extension (e.g., ".m4a", ".flac"), used to determine
            if a legacy lossless rip is explicitly requested.

    Returns: The master file and its metadata
    """

    source_spec = _resolve_makemkv_src_spec(src_path)
    if source_spec:
        # Handle formats that require MakeMKV rips (Blu-rays, Blu-ray images, Blu-ray backup directories)
        title_idx, matched_candidate, atmos_streams, disc_topology = (
            find_primary_title(source_spec, artist, album, output_container == ".flac"))
        atmos_mkv = rip_title_to_mkv(source_spec, TMP_DIR, title_idx)
        chapters, duration = extract_chapters_and_duration_from_mkv(atmos_mkv)
    else:
        # Handle other formats, which bypass MakeMKV (IAA directory of mkv or mp4 tracks, Headphone Dust single mkv
        src_p = Path(src_path)
        if src_p.is_dir():  # Folder of mkv or mp4 files (IAA)
            atmos_mkv = merge_folder_to_master_mkv(src_p, TMP_DIR)
        else:  # Single MKV file (Headphone Dust)
            atmos_mkv = src_p.resolve()
            if not atmos_mkv.exists():
                raise FileNotFoundError(f"Not found: {src_path}")

        # Intersect local MKV chapters with MusicBrainz candidates
        candidates = fetch_candidate_metadata(artist, album)
        disc_topology = generate_topology_from_mkv(atmos_mkv)
        sole_title = disc_topology["0"]
        chapter_count = sole_title.num_chapters
        chapters, duration = extract_chapters_and_duration_from_mkv(atmos_mkv)
        title_idx = "0"  # Sole chapter index
        matched_candidate = get_best_mb_candidate(artist, album, chapter_count, duration, candidates)
        atmos_streams = sole_title.atmos_streams
        source_spec = None  # No disc to re-rip.

    return AcquisitionResult(master_mkv_path=atmos_mkv, matched_candidate=matched_candidate, chapters=chapters,
                             duration=duration, atmos_streams=atmos_streams, disc_topology=disc_topology,
                             title_idx=title_idx, mkv_src_spec=source_spec)


def generate_topology_from_mkv(mkv_path: Path) -> dict[str, TitleInfo]:
    """Generates a synthetic Carat topology dictionary from a single MKV file."""

    # 1. Ask ffprobe for the FORMAT, streams, and chapters in JSON format
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", str(mkv_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    probe_data = json.loads(result.stdout)

    # 2. Parse the chapters
    chapters = probe_data.get("chapters", [])
    chapter_count = len(chapters)

    # 3. Parse the duration (Safely cast string -> float -> int)
    duration_str = probe_data.get("format", {}).get("duration")
    if duration_str:
        duration_sec = int(float(duration_str))
    elif chapters:
        duration_sec = int(float(chapters[-1].get("end_time", 0.0)))
    else:
        duration_sec = 0

    # 4. Parse the audio streams
    streams = {}
    atmos_streams: set[int] = set()

    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") == "audio":
            idx = stream.get("index")
            codec = stream.get("codec_name", "unknown")
            channels = stream.get("channels", 2)
            layout = stream.get("channel_layout")

            if not layout:
                channel_map = {8: "7.1", 6: "5.1", 2: "2.0", 1: "1.0"}
                layout = channel_map.get(channels, f"{channels}.0")
            else:
                # Normalize plain-text layouts to match MakeMKV's numeric format
                if layout == "stereo":
                    layout = "2.0"
                elif layout == "mono":
                    layout = "1.0"

            # Build the descriptive string (e.g., "TRUEHD 7.1")
            desc = f"{codec.upper()} {layout}"
            streams[idx] = desc

            profile = stream.get("profile", "").lower()
            tags = stream.get("tags", {})
            title = tags.get("title", "").lower()

            if "atmos" in profile or "atmos" in title:
                atmos_streams.add(idx)

    # 5. Return a fabricated topology dictionary with exactly one Title
    synthetic_title = TitleInfo(
        num_chapters=chapter_count,
        duration_sec=duration_sec,
        streams=streams,
        atmos_streams=atmos_streams
    )

    return {"0": synthetic_title}


def _resolve_makemkv_src_spec(src: str) -> str | None:
    """
    Converts a raw source path into a MakeMKV-compatible target string.
    Returns None if the source bypasses MakeMKV (e.g., standalone MKVs or IAA folders).
    """
    try:
        drive_idx = int(src)
        if drive_idx == -1:
            res = run_command([TOOLS.MAKEMKV, "-r", "info", "disc:0"])
            if "BD-RE" in res or "BD-ROM" in res:
                drive_idx = 0
        return f"disc:{drive_idx}"
    except (ValueError, TypeError):
        src_p = Path(src)
        if src_p.suffix.lower() == ".iso":
            return f"iso:{src_p.resolve()}"
        elif src_p.is_dir() and (src_p / "BDMV").exists():
            return f"file:{src_p.resolve() / 'BDMV'}"

    return None


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

    # Gather non-invasive environment data
    os_info = f"{platform.system()} {platform.release()} ({platform.machine()})"
    py_info = f"Python {platform.python_version()}"

    logger.emit(f"Date/Time : {start_time_str}")
    logger.emit(f"Carat Ver : {__version__}")
    logger.emit(f"Platform  : {os_info} | {py_info}")
    logger.emit(f"Source    : {src_path}")
    logger.emit(f"Artist    : {artist}")
    logger.emit(f"Album     : {album}")
    logger.emit(f"Format    : {output_container.upper()} (Codec: {preferred_codec})")
    logger.emit("=====================\n")


class AudioProfile(NamedTuple):
    """
    The resolved audio stream and destination formatting for a ripping job.

    Attributes:
        stream_idx (int): The FFmpeg stream index of the selected audio stream in the master file.
        container (str): The target file extension for the output (e.g., '.m4a', '.flac').
        suffix (str): The descriptive tag appended to the final filename (e.g., '(Atmos)', '(Surround)').
            For stereo tracks, this will be the empty string.
    """
    stream_idx: int
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
            return AudioProfile(stream_idx=idx, container=requested_container, suffix=" (Atmos)")

        logger.emit("\n[!] No Dolby Atmos stream found on source.")
        logger.emit("[*] Automatically switching to FLAC slicing for legacy lossless surround...")

    # 2. Fallback to Legacy Multichannel (or proceed if FLAC was explicitly requested)
    legacy_info = find_multichannel_stream(master_mkv)
    if legacy_info is None:
        raise ValueError("No compatible audio stream (LPCM, DTS-HD MA, TrueHD, MLP, alac, flac) found in master file.")
    idx, channels = legacy_info

    if channels == 1:
        suffix = " (Mono)"
    elif channels == 2:
        suffix = ""  # The implicit standard
    elif channels == 4:
        suffix = " (Quad)"
    else:
        # Catches 3.0, 5.0, 5.1, 7.1, etc.
        suffix = " (Surround)"

    return AudioProfile(stream_idx=idx, container=".flac", suffix=suffix)


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
    info: MusicBrainzMetadata  # The matched MusicBrainz release metadata
    chapters: list[MkvChapter]  # The extracted chapter markers
    tracks: list[MusicBrainzTrack]  # The tracklist metadata from MusicBrainz


def _assemble_gapless_album(ctx: RipContext) -> None:
    """Handles the FFmpeg remuxing for gapless M4A or MKV containers."""
    final_audio_name = f"{ctx.clean_album}{ctx.profile.suffix}{ctx.profile.container}"

    if ctx.tracks:
        ctx.chapters = ctx.chapters[:len(ctx.tracks)]

    generate_cue_sheet(ctx.dest / f"{ctx.clean_album}{ctx.profile.suffix}.cue", final_audio_name, ctx.info,
                       ctx.chapters, ctx.tracks)

    cmd = [
        TOOLS.FFMPEG, "-hide_banner", "-loglevel", "error", "-stats",
        "-probesize", "100M", "-analyzeduration", "100M",
        "-i", str(ctx.master_mkv), "-map", f"0:{ctx.profile.stream_idx}",
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

    run_command(cmd, f"Finalizing{ctx.profile.suffix} {ctx.profile.container[1:].upper()}",
                {"ffmpeg_duration": ctx.duration})

def _extract_flac_tracks(ctx: RipContext) -> list[tuple[Path, dict, int]]:
    """Handles slicing the master MKV into individual FLAC tracks."""
    logger.emit(f"[*] Slicing tracks to FLAC{ctx.profile.suffix}...")
    if ctx.profile.stream_idx is None:
        raise ValueError("No compatible audio stream (LPCM, DTS-HD MA, TrueHD, MLP, alac, flac) found in master file.")

    flac_files = []
    for i, (track, chapter) in enumerate(zip(ctx.tracks, ctx.chapters)):
        track_num = i + 1
        safe_title = _sanitize_filename(track['title'])
        out_filename = f"{track_num:02d} {safe_title}.flac"
        out_path = ctx.dest / out_filename
        flac_files.append((out_path, track, track_num))

        start_time = chapter['start_time']
        end_time = chapter['end_time']

        cmd = [
            TOOLS.FFMPEG, '-hide_banner', '-loglevel', 'error', '-stats', '-y',
            '-ss', str(start_time), '-to', str(end_time),
            '-i', str(ctx.master_mkv),
            '-map', f"0:{ctx.profile.stream_idx}",
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


def rip_album_to_library(src: str, artist: str, album: str, library_root: str,
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
        src: The polymorphic source path (Disc index, ISO, BDMV folder, MKV, or IAA folder).
        artist: The requested album artist (used for metadata fetching).
        album: The requested album title (used for metadata fetching).
        library_root: The destination directory for the processed album.
        output_container: The target container format (.m4a, .mkv, or .flac).
        preferred_codec: The target Atmos codec (truehd, eac3, or ac3).

    Returns:
        bool: True if tracklist metadata was successfully found and applied, False otherwise.
    """
    plan = prepare_album_rip(src, artist, album, library_root, output_container, preferred_codec)
    logger.emit("Proceeding with Carat heuristic rip...")
    try:
        return execute_album_rip(plan)
    finally:
        clean_up()


def prepare_album_rip(src: str, artist: str, album: str, library_root: str,
                      output_container: Container = Container.M4A, preferred_codec: Codec = Codec.TRUEHD) -> RipPlan:
    """
        Phase 1 of the extraction pipeline: Scans the source, acquires physical media, and formulates a RipPlan.

        This function performs all the heavy I/O and network scouting required before an album can be
        assembled. It extracts the raw, unencrypted master MKV file to a temporary directory, parses the
        disc's topological chapter/stream data, and queries MusicBrainz for matching metadata.

        If successful, it leaves the temporary MKV intact on the disk and returns a `RipPlan` object.
        This object can be inspected or modified by a GUI/client before being passed to `execute_album_rip`
        for final assembly.

        Note:
            For comprehensive details on the input parameters and polymorphic source handling,
            see the docstring for `rip_album_to_library`.

        Args:
            src: The polymorphic source path (Disc index, ISO, BDMV folder, MKV, or IAA folder).
            artist: The requested album artist.
            album: The requested album title.
            library_root: The destination directory, verified for write access.
            output_container: The target container format (.m4a, .mkv, or .flac).
            preferred_codec: The target Atmos codec (truehd, eac3, or ac3).

        Returns:
            RipPlan: A blueprint containing the proposed heuristics and opaque runtime state.

        Raises:
            RuntimeError: If MakeMKV fails, the disc is unreadable, or no matching titles are found.
            PermissionError: If the library_root is not writable.
        """
    ingestion_start_time = time.time()
    _nuke_dir(TMP_DIR)  # In case there's any garbage lying around from a previous rip in this session
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    lib_path = Path(library_root)
    _ensure_writable(lib_path)
    logger.open_log_file(TMP_DIR / "carat_rip.log") # We log to tmp dir because we don't yet know the final destination
    _log_prologue(album, artist, output_container, preferred_codec, src)

    # This log_dest value is tentative. It will be used only if the rip fails early on; otherwise it will be overwritten
    clean_artist = _sanitize_filename(artist)
    clean_album = _sanitize_filename(album)
    log_name = f"{clean_artist} - {clean_album}.log".replace("  ", " ").strip()
    log_dest = lib_path / clean_artist / clean_album / log_name

    try:
        ar: AcquisitionResult = get_mkv_master_file_and_metadata(src, artist, album, output_container)
        profile = resolve_audio_profile(ar.master_mkv_path, ar.atmos_streams, output_container, preferred_codec)

        # Canonicalize artist and album names if indicated by results of call to get_mkv_master_file_and_metadata
        canonical_artist = artist
        canonical_album = album
        if ar.matched_candidate:
            canonical_artist = ar.matched_candidate.get('artist', artist)
            canonical_album = ar.matched_candidate.get('title', album)
            if (artist != canonical_artist) or (album != canonical_album):
                logger.emit(f"[+] Canonicalized as Artist: {canonical_artist}, Album: {canonical_album}")
        else:
            logger.emit(f"[!] No MusicBrainz metadata found. Proceeding with given artist and album names.")

        # Sanitize artist and album names for safe use in file path names, if necessary
        clean_artist = _sanitize_filename(canonical_artist)
        clean_album = _sanitize_filename(canonical_album)
        if (clean_artist != canonical_artist) or (clean_album != canonical_album):
            logger.emit(f"[+] Sanitized for filesystem as Artist: {clean_artist}, Album: {clean_album}")

        log_name = f"{clean_artist} - {clean_album}{profile.suffix}.log".replace("  ", " ").strip()
        log_dest = lib_path / clean_artist / f"{clean_album}{profile.suffix}" / log_name

        return RipPlan(
            artist=clean_artist,
            album=clean_album,
            matched_candidate=ar.matched_candidate,
            audio_profile=profile,
            disc_topology = ar.disc_topology,
            title_idx=ar.title_idx,

            _internals=_RipPlanInternals(
                master_mkv_path=ar.master_mkv_path,
                log_dest_path=log_dest,
                lib_path=lib_path,
                src=src,
                output_container=output_container,
                ingestion_start_time=ingestion_start_time,
                makemkv_streams=ar.atmos_streams,
                duration=ar.duration,
                chapters=ar.chapters,
                mkv_src_spec=ar.mkv_src_spec
            )
        )
    except Exception as e:
        logger.emit(f"\n[!] Preparation aborted: {e}")
        log_dest.parent.mkdir(parents=True, exist_ok=True)
        logger.close_log_file(log_dest)
        clean_up()
        raise

# noinspection PyProtectedMember
def execute_album_rip(plan: RipPlan) -> bool:
    """
        Phase 2 of the extraction pipeline: Assembles, tags, and finalizes the audio files.

        This function consumes a `RipPlan` (typically generated by `prepare_album_rip` or modified via
        `RipPlan.revise()`) and executes the final FFmpeg remuxing or slicing operations. It downloads
        the highest quality cover art, canonicalizes the directory names, writes the gapless CUE sheets,
        and injects the MusicBrainz metadata tags.

        This function strictly guarantees cleanup. Whether the extraction succeeds, fails, or crashes,
        the `finally` block ensures the massive temporary master MKV is deleted and the log file is
        moved to its final destination in the user's library.

        Args:
            plan: The immutable blueprint containing the user-approved heuristics and physical file paths.

        Returns:
            bool: True if tracklist metadata was successfully found and applied, False if the engine
                  had to fall back to generic "Track <#>" naming.
    """
    matched_candidate = plan.matched_candidate
    profile = plan.audio_profile
    artist = plan.artist
    album = plan.album
    info = matched_candidate or MusicBrainzMetadata(title=album, artist=artist, year='unknown', mbid='unknown', medium_index=1, tracks=[])
    log_dest = plan._internals.log_dest_path
    dest = log_dest.parent
    logger.emit(f"[+] Executing Rip for: {artist} - {album}")

    try:
        dest.mkdir(parents=True, exist_ok=True)
        ctx = RipContext(
            master_mkv=plan._internals.master_mkv_path, dest=dest, artist=artist, album=album,
            clean_album=(_sanitize_filename(album)), profile=profile, duration=plan._internals.duration, info=info,
            chapters=plan._internals.chapters, tracks=(matched_candidate.get('tracks', []) if matched_candidate else [])
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
        logger.emit(f"[+] Total elapsed time: {(time.time() - plan._internals.ingestion_start_time):.1f} seconds.")
    finally:
        logger.close_log_file(log_dest)

    return bool(matched_candidate)


@dataclass(frozen=True)
class _RipPlanInternals:
    """Opaque runtime state for a pending extraction.

    This object encapsulates the physical file paths, timers, and raw MakeMKV stream data generated during the
    preparation phase. It is strictly for internal use by the execution engine.

    Attributes:
        master_mkv_path: The Path to the consolidated, unencrypted MKV file extracted during Phase 1
        log_dest_path: Preliminary destination file for the log (will change if canonicalization
            affects artist or album title).
        lib_path: The verified target base directory for the user's music library.
        src: The original source provided by user (for accurate logging)
        mkv_src_spec: # The validated MakeMKV target (used for re-extraction), or None if MKV extraction bypassed.
        output_container: The requested target file extension (e.g., '.m4a', '.flac')
        ingestion_start_time: The epoch timestamp when the preparation phase began,
            used for calculating total pipeline duration.
        makemkv_streams: A set of absolute stream indices that MakeMKV explicitly
            identified as containing Atmos metadata.
        duration: The total duration of the source in seconds
        chapters: The extracted chapter markers
    """
    master_mkv_path: Path
    log_dest_path: Path
    lib_path: Path
    src: str
    mkv_src_spec: str | None
    output_container: str
    ingestion_start_time: float
    makemkv_streams: set[int]
    duration: float
    chapters: list[MkvChapter]

@dataclass(frozen=True)
class RipPlan:
    """A blueprint for extracting and tagging an album.

    Generated by `prepare_album_rip` (or our own 'revise' function), this object acts as the boundary between
    the heuristic engine and the final execution pipeline. The public attributes represent the "Winning Hand" chosen
    by Carat's heuristics (or our own 'revise' function).

    Attributes:
        artist: The canonicalized artist name, to be used in metadata and (once canonicalized) file paths.
        album:  The canonicalized album title, to be used in metadata and (once canonicalized) file paths.
        matched_candidate: The MusicBrainz release dictionary containing the tracklist and metadata tags.
        audio_profile: The AudioProfile object detailing the selected codec and stream index to be extracted.
        disc_topology: A dictionary containing the disc topology parsed from MakeMKV.
        title_idx: The index of the title that was extracted from the input source (None if source bypassed MakeMKV).
    """
    artist: str
    album: str
    audio_profile: AudioProfile
    matched_candidate: MusicBrainzMetadata | None
    disc_topology: dict[str, TitleInfo]
    title_idx: str

    _internals: _RipPlanInternals

    @property
    def target_dir(self) -> Path:
        """Returns the fully resolved Path object for the final destination directory."""
        return self._internals.log_dest_path.parent

    def revise(self, new_title_idx: str, new_stream_idx: int, new_mbid: str, new_medium_idx: int,
               new_artist: str, new_album: str, start_new_log: bool = True) -> 'RipPlan':
        """
        Validates user overrides, fetches revised metadata, and generates a new extraction plan.

        If the `new_title_idx` differs from the current plan, this method will validate the target chapter counts
        against the cached disc topology. If the validation passes, it performs a synchronous physical extraction of
        the new title from the source drive to replace the temporary master MKV file.

         Args:
             new_title_idx: The MakeMKV index of the newly selected Blu-ray title.
             new_stream_idx: The absolute MakeMKV/FFmpeg index of the selected audio stream.
             new_mbid: The MusicBrainz Release ID (MBID) for the desired metadata.
             new_medium_idx: The specific medium (disc) number within the MusicBrainz release.
             new_artist: Artist to be substituted (locally) for the one obtained from MusicBrainz.
             new_album: Album title to be substituted (locally) for the one obtained from MusicBrainz.
             start_new_log: Whether to start a new log file for the revision or continue using the existing one.

         Returns:
             RipPlan: A new rip plan containing the updated state

         Raises:
             ValueError: If the requested title or medium index does not exist, or if the source
                 topology is missing (e.g., if the original source was a folder of loose audio files).
             TrackMismatchError: If the chapter count of the newly selected physical title does
                 not perfectly match the track count of the selected metadata.
         """

        # ==========================================
        # 1. STATE DIFFING
        # ==========================================
        old_mbid = self.matched_candidate['mbid'] if self.matched_candidate else None
        old_medium = self.matched_candidate['medium_index'] if self.matched_candidate else None

        mbid_changed = (new_mbid != old_mbid)
        medium_changed = (new_medium_idx != old_medium)
        metadata_fetch_needed = mbid_changed or medium_changed
        artist_changed = (new_artist != self.artist)
        album_changed = (new_album != self.album)
        title_changed = (new_title_idx != self.title_idx)
        stream_changed = (new_stream_idx != self.audio_profile.stream_idx)

        # ==========================================
        # 2. LOGGING PROLOGUE
        # ==========================================
        internals = self._internals
        if start_new_log:
            new_tmp_log = internals.master_mkv_path.parent / f"carat_rip_revised_{time.time()}.log"
            logger.open_log_file(new_tmp_log)

        def _flag(changed: bool) -> str:
            return "*" if changed else " "

        logger.emit("=== Carat Rip Log (Revised Extraction) ===")
        logger.emit(f"{_flag(artist_changed)} Artist         : {new_artist}")
        logger.emit(f"{_flag(album_changed)} Album          : {new_album}")
        logger.emit(f"{_flag(title_changed or stream_changed)} Data Source    : "
                    f"{internals.src} (Title {new_title_idx}, Stream {new_stream_idx})")
        logger.emit(f"{_flag(metadata_fetch_needed)} Metadata Source: MBID {new_mbid} (Medium {new_medium_idx})")
        logger.emit("==========================================\n")

        # ==========================================
        # 3. TOPOLOGY & METADATA VALIDATION
        # ==========================================
        title_info = self.disc_topology.get(new_title_idx)
        if not title_info:
            raise ValueError(f"Title {new_title_idx} does not exist on this disc.")

        if metadata_fetch_needed:
            new_candidate = fetch_metadata(new_mbid, new_medium_idx)
        else:
            if not self.matched_candidate:
                raise ValueError("No existing metadata to reuse, but fetch was bypassed.")
            new_candidate = self.matched_candidate

        # Enforce chapter count matching
        mb_track_count = len(new_candidate['tracks'])
        if (title_changed or metadata_fetch_needed) and title_info.num_chapters != mb_track_count:
            raise TrackMismatchError(f"Cannot revise: Title {new_title_idx} has {title_info.num_chapters} chapters, "
                                     f"but metadata has {mb_track_count} tracks.")

        # ==========================================
        # 4. FINAL NAMING STATE & SLOW RE-RIP
        # ==========================================
        clean_artist = _sanitize_filename(new_artist)
        clean_album = _sanitize_filename(new_album)
        if (clean_artist != new_artist) or (clean_album != new_album):
            logger.emit(f"[+] Sanitized for filesystem as Artist: {clean_artist}, Album: {clean_album}")

        new_mkv = internals.master_mkv_path
        new_chapters = internals.chapters
        new_duration = internals.duration
        new_atmos_streams = internals.makemkv_streams

        if title_changed:
            if not internals.mkv_src_spec:
                raise ValueError("Cannot perform re-rip: source_spec is missing from internal state.")
            logger.emit(f"[*] Extracting New Title {new_title_idx} from Blu-ray or image...")

            if internals.master_mkv_path.exists():
                internals.master_mkv_path.unlink()
            tmp_dir = internals.master_mkv_path.parent
            new_mkv = rip_title_to_mkv(internals.mkv_src_spec, tmp_dir, new_title_idx)
            new_chapters, new_duration = extract_chapters_and_duration_from_mkv(new_mkv)
            new_atmos_streams = title_info.atmos_streams
        else:
            logger.emit("\n[*] Processing revised rip plan...")

        # ==========================================
        # 5. SUFFIX GENERATION & FORMAT RESOLUTION
        # ==========================================
        stream_desc = title_info.streams.get(new_stream_idx, "")
        suffix = _suffix_for_stream_desc(stream_desc)

        is_atmos_stream = (new_stream_idx in new_atmos_streams)
        if is_atmos_stream and internals.output_container != ".flac":
            final_container = internals.output_container
        else:
            final_container = ".flac"

        new_profile = AudioProfile(
            stream_idx=new_stream_idx,
            container=final_container,
            suffix=suffix
        )

        log_name = f"{clean_artist} - {clean_album}{suffix}.log".replace("  ", " ").strip()
        new_log_dest = internals.lib_path / clean_artist / f"{clean_album}{suffix}" / log_name

        new_internals = replace(internals, master_mkv_path=new_mkv, log_dest_path=new_log_dest,
                                ingestion_start_time=time.time(), makemkv_streams=new_atmos_streams,
                                duration=new_duration, chapters=new_chapters)

        return RipPlan(artist=new_artist, album=new_album, audio_profile=new_profile, matched_candidate=new_candidate,
                       disc_topology=self.disc_topology, title_idx=new_title_idx, _internals=new_internals)

    def with_suffix(self, custom_suffix: str) -> 'RipPlan':
        """Creates a copy of the plan with a new suffix, bypassing metadata fetches."""
        from dataclasses import replace
        custom_suffix = custom_suffix.strip()
        if not custom_suffix:
            logger.emit("[-] Custom suffix is empty. Using existing suffix.")
            return self

        suffix = f" {custom_suffix}" if custom_suffix.startswith("(") else f" ({custom_suffix})"

        new_profile = AudioProfile(
            stream_idx=self.audio_profile.stream_idx,
            container=self.audio_profile.container,
            suffix=suffix
        )

        internals = self._internals
        clean_artist = self.artist
        clean_album = self.album
        log_name = f"{clean_artist} - {clean_album}{suffix}.log".replace("  ", " ").strip()
        new_log_dest_path = internals.lib_path / clean_artist / f"{clean_album}{suffix}" / log_name

        new_internals = replace(internals, log_dest_path=new_log_dest_path)
        return replace(self, audio_profile=new_profile, _internals=new_internals)


class TrackMismatchError(Exception):
    """Raised when user-selected metadata track count doesn't match selected title chapter count."""
    pass


def _suffix_for_stream_desc(stream_desc: str) -> str:
    """
    Determines the appropriate file suffix based on a MakeMKV stream description.
    """
    desc_lower = stream_desc.lower()

    if "atmos" in desc_lower or "truehd" in desc_lower:
        return " (Atmos)"
    elif "quad" in desc_lower or "4.0" in desc_lower:
        return " (Quad)"
    elif "stereo" in desc_lower or "2.0" in desc_lower:
        return ""
    elif "surround" in desc_lower or "5.0" in desc_lower or "5.1" in desc_lower or "7.1" in desc_lower:
        return " (Surround)"

    return " (Unknown)"


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
