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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, NoReturn

import musicbrainzngs
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

            if code == "0":
                if attr_id == "8":
                    titles[t_idx].chapters = int(parts[3].strip('"'))
                elif attr_id == "11":
                    titles[t_idx].size = int(parts[3].strip('"'))

        # MakeMKV SINFO Format: SINFO:title_idx,stream_idx,attribute_id,code,"value"
        if line.startswith("SINFO:"):
            if "A_TRUEHD" in line or "TrueHD Atmos" in line:
                titles[t_idx].score = max(titles[t_idx].score, 1000)
                logger.emit(f"    [*] Title {t_idx}: Found Lossless Atmos (+1000)")
            elif "A_EAC3" in line and "Atmos" in line:
                titles[t_idx].score = max(titles[t_idx].score, 500)
                logger.emit(f"    [*] Title {t_idx}: Found Lossy Atmos (+500)")

    return titles


def get_best_mb_candidate(chapter_count: int, candidates: list[dict]) -> dict | None:
    """
    Finds the best MusicBrainz candidate for a given number of chapters.
    Filters for exact matches or +1 preamble matches, prioritizing exact matches.
    """
    if not candidates:
        return None

    # Filter for valid matches (exact or +1 preamble)
    matched = [c for c in candidates if 0 <= (chapter_count - len(c['tracks'])) <= 1]

    if matched:
        # Sort so exact matches (diff=0) win.
        # Python's stable sort automatically preserves the original MB relevance rank for ties!
        matched.sort(key=lambda c: chapter_count - len(c['tracks']))
        return matched[0]

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
                          "Surgical Atmos Scan")
        titles = parse_makemkv_info(res)
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
            best_candidate = get_best_mb_candidate(info.chapters, candidates)

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
    logger.emit(f"[*] Winner: Title {winner_idx} (Rank: {w_rank}, Track count Δ: {w_diff}, Size: {-w_neg_size} bytes, Score: {titles[winner_idx].score})")

    return winner_idx, matched_candidate


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
    logger.emit(f"[+] Title extraction complete: {size_mb:.1f} MB in {elapsed:.1f} seconds (Avg: {size_mb / elapsed:.1f} MB/s)")

    return winner


def find_atmos_stream(mkv_path: Path) -> int | None:
    """
    Returns the index of the highest quality Atmos stream, with TrueHD (lossless) prioritized over EAC3-JOC (lossy),
    from the given mkv file, or None if neither are found. Emits a warning if we have to settle for EAC3-JOC.
    """
    cmd = [TOOLS.FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index,channels,codec_name",
           "-of", "json", str(mkv_path)]
    res = run_command(cmd, "Scanning for Atmos Stream")
    try:
        streams = json.loads(res).get('streams', [])

        # 1. Try to find Lossless TrueHD first
        truehd_candidates = [s for s in streams if s.get('codec_name') == 'truehd']
        if truehd_candidates:
            return int(max(truehd_candidates, key=lambda x: int(x.get('channels', 0)))['index'])

        # 2. Fall back to Lossy E-AC-3-JOC
        eac3_candidates = [s for s in streams if s.get('codec_name') == 'eac3']
        if eac3_candidates:
            # Universal warning for all input formats
            logger.emit("[!] =========================================================")
            logger.emit("[!] WARNING: No TrueHD found! Falling back to lossy EAC3-JOC.")
            logger.emit("[!] =========================================================")
            return int(max(eac3_candidates, key=lambda x: int(x.get('channels', 0)))['index'])

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


# Maximum number of release groups to search in MusicBrainz when look for the album
MAX_RELEASE_GROUPS: int = 5


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


def fetch_candidate_metadata(artist: str, album: str) -> list[dict[str, Any]]:
    """
    Fetches candidates from MusicBrainz, filtering out releases that don't match
    the requested artist and album name using a fuzzy similarity guardrail.
    """
    candidates = []
    logger.emit(f"[*] Fetching MusicBrainz candidates for: {artist} - {album}")

    try:
        # 1. Search for the Release Group
        query = f'artist:"{artist}" AND release:"{album}"'
        res = musicbrainzngs.search_release_groups(query=query, limit=5)

        if not res.get('release-group-list'):
            logger.emit("    [-] No release groups found.")
            return []

        # 2. Filter & Fetch Tracks
        for rg in res['release-group-list']:
            found_artist = rg['artist-credit'][0]['artist']['name']
            found_album = rg['title']

            # Guardrail: Prevent wildly incorrect matches
            if not (_is_safe_match(artist, found_artist) and _is_safe_match(album, found_album)):
                logger.emit(
                    f"    [-] Rejected MB Candidate: {found_artist} - {found_album} (Failed similarity guardrail)")
                continue

            logger.emit(f"    [+] Evaluating MB Candidate: {found_artist} - {found_album}")

            # Fetch releases for this group to get tracklists
            rg_info = musicbrainzngs.get_release_group_by_id(rg['id'], includes=['releases'])
            releases = rg_info.get('release-group', {}).get('release-list', [])

            if not releases: continue

            # Grab the tracklist for the first release in the group
            release_id = releases[0]['id']
            rel_info = musicbrainzngs.get_release_by_id(release_id, includes=['recordings'])

            all_tracks = []
            for medium in rel_info.get('release', {}).get('medium-list', []):
                for track in medium.get('track-list', []):
                    all_tracks.append({
                        'title': track.get('recording', {}).get('title', 'Unknown Track'),
                        'duration': track.get('recording', {}).get('length', 0)
                    })

            if all_tracks:
                candidates.append({
                    'title': found_album,
                    'artist': found_artist,
                    'year': rg.get('first-release-date', '')[:4] or 'Unknown',
                    'mbid': rg['id'],
                    'tracks': all_tracks
                })

    except musicbrainzngs.WebServiceError as e:
        logger.emit(f"    [!] MusicBrainz API Error: {e}")
    except Exception as e:
        logger.emit(f"    [!] Unexpected error fetching from MusicBrainz: {e}")

    if candidates:
        counts = {len(c['tracks']) for c in candidates}
        logger.emit(f"    [+] Found valid MB candidates with track counts: {counts}")
    else:
        logger.emit("    [-] No valid candidates found after filtering.")

    return candidates


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
        if src_p.is_dir(): # Folder of mkv or mp4 files (IAA)
            atmos_mkv = merge_folder_to_master_mkv(src_p, TMP_DIR)
        else:              # Single MKV file (Headphone Dust)
            atmos_mkv = src_p.resolve()
            if not atmos_mkv.exists():
                raise FileNotFoundError(f"Not found: {src_path}")

        # Intersect local MKV chapters with MusicBrainz candidates
        chapters, duration = extract_chapters_and_duration_from_mkv(atmos_mkv)
        candidates = fetch_candidate_metadata(artist, album)
        matched_candidate = get_best_mb_candidate(len(chapters), candidates)

        # Fallback: if no strict match was found but we HAVE candidates, just blindly trust the top result
        if not matched_candidate and candidates:
            matched_candidate = candidates[0]

    return atmos_mkv, matched_candidate, chapters, duration


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
        target = lib_path / clean_artist / f"{clean_album} (Atmos)"
        target.mkdir(parents=True, exist_ok=True)

        # 3. Final Assembly (Concurrent)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            cover_future = ex.submit(get_cover_art.download_cover_art, artist, album, target, mbid)

            generate_cue_sheet(target / f"{clean_album} (Atmos).cue",
                               f"{clean_album} (Atmos).m4a", info, chapters, tracks)
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
