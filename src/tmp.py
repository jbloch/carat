"""
carat.py - The core logic for the Concise Atmos Ripping Automation Tool.
Handles disc probing, MKV ripping, TrueHD extraction, and MusicBrainz metadata.
"""
import sys
import json
import subprocess
import shutil
import tempfile
import concurrent.futures
import argparse
import re
import platform
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Callable

import getcoverart
import musicbrainzngs as mb

__all__ = ['process_release']

mb.set_useragent("CaratAudioTool", "1.0", "josh@bloch.us")

# --- (1) The Porcelain (Metadata & Utils) ---

def _no_op_logger(msg: str, is_progress: bool = False) -> None:
    """Default consumer to avoid 'if callback:' clutter."""
    pass

def cli_logger(msg: str, is_progress: bool = False) -> None:
    """A CLI-specific logger that handles carriage returns for progress."""
    if is_progress:
        sys.stdout.write(f"\r    {msg}")
        sys.stdout.flush()
    else:
        # Clear the progress line if a new standard message comes in
        sys.stdout.write(f"\n{msg}\n" if msg.startswith("[*]") else f"{msg}\n")

def seconds_to_cue(seconds: float) -> str:
    """Converts seconds to MM:SS:FF for gapless CUE sheets."""
    return f"{int(seconds//60):02d}:{int(seconds%60):02d}:{int((seconds%1)*75):02d}"

def generate_cue_sheet(cue_path: Path, m4a_name: str, info: Dict, chapters: List, mb_tracks: List) -> None:
    """Generates the CUE sheet for gapless playback."""
    with cue_path.open('w', encoding='utf-8') as f:
        f.write(f'PERFORMER "{info["artist"]}"\nTITLE "{info["title"]} (Atmos)"\nREM DATE {info.get("year", "Unknown")}\nFILE "{m4a_name}" WAVE\n')
        for i, ch in enumerate(chapters):
            title = mb_tracks[i]['recording']['title'] if i < len(mb_tracks) else f"Track {i+1}"
            f.write(f'  TRACK {i+1:02d} AUDIO\n    TITLE "{title}"\n    INDEX 01 {seconds_to_cue(float(ch["start_time"]))}\n')

def _parse_makemkv_msg(line: str) -> Optional[str]:
    """Derobotifies MakeMKV MSG templates into human-readable text."""
    if not line.startswith("MSG:"): return None
    try:
        parts = re.findall(r'[^,",]+|"[^"]*"', line)
        if len(parts) < 4: return None
        template = parts[3].strip('"')
        params = [p.strip('"') for p in parts[4:]]
        for i, val in enumerate(params, 1):
            template = template.replace(f"%{i}", val)
        return template
    except Exception: return None

# --- (2) The Plumbing (Clean Subprocess) ---

def run_command(cmd: List[str], desc: Optional[str] = None, log_callback: Callable = _no_op_logger, capture: bool = False) -> str:
    """Executes command with live progress updates, catching WinError cleanup issues."""
    if desc and not capture: log_callback(f"[*] {desc}...")

    creationflags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=creationflags)

    output_acc = []

    for line in process.stdout:
        line = line.strip()
        if not line: continue
        output_acc.append(line)

        if not capture:
            # [1] MakeMKV Progress
            if any(x in line for x in ["PRGV:", "PRGC:", "PRGT:"]):
                try:
                    parts = line.split(":")[1].split(",")
                    pct = (float(parts[0]) / float(parts[2])) * 100
                    if 0 <= pct <= 100:
                        log_callback(f"Copying: {pct:.1f}%", is_progress=True)
                except Exception: pass

            # [2] FFmpeg Progress
            elif "size=" in line and "time=" in line and "bitrate=" in line:
                log_callback(f"Transcoding: {line}", is_progress=True)

            # [3] Standard Output / Filtered Chatter
            else:
                msg = _parse_makemkv_msg(line)
                if msg:
                    log_callback(f"[*] {msg}")
                elif not any(line.startswith(x) for x in ["DRV:", "TDRV:", "CIDC:", "SINFO:", "TINFO:", "CINFO:", "PRGC:", "PRGT:", "PRGV:"]):
                    log_callback(line)

    process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"Command failed (Code {process.returncode}): {' '.join(cmd)}")

    return "\n".join(output_acc)

# --- (3) Toolset & Logic ---

class Toolset:
    def __init__(self) -> None:
        self.IS_WIN = platform.system() == "Windows"
        self.FFMPEG = self._find("ffmpeg", [r"C:\ffmpeg\bin\ffmpeg.exe", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"])
        self.FFPROBE = self._find("ffprobe", [r"C:\ffmpeg\bin\ffprobe.exe", "/usr/local/bin/ffprobe", "/opt/homebrew/bin/ffprobe"])
        self.MKVMERGE = self._find("mkvmerge", [r"C:\Program Files\MKVToolNix\mkvmerge.exe", "/usr/local/bin/mkvmerge", "/opt/homebrew/bin/mkvmerge"])
        self.MAKEMKV = self._find("makemkvcon64" if self.IS_WIN else "makemkvcon", [
            r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
            "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
            "/usr/bin/makemkvcon"
        ])
        self._validate()

    def _find(self, name: str, prospects: List[str] = []) -> Optional[str]:
        found = shutil.which(name)
        if found: return found
        for p in prospects:
            if Path(p).exists(): return str(Path(p))
        return None

    def _validate(self) -> None:
        if any(v is None for v in self.__dict__.values() if not isinstance(v, bool)):
            missing = [k for k,v in self.__dict__.items() if v is None]
            print(f"ERROR: Missing dependencies: {missing}")
            sys.exit(1)

TOOLS = Toolset()

def find_primary_title(source_spec: str, log_callback: Callable) -> str:
    """Identifies the Atmos title using surgical MakeMKV scanning."""
    res = run_command([TOOLS.MAKEMKV, "--progress=-stdout", "-r", "info", source_spec, "--minlength=600"], "Surgical Atmos Scan", log_callback, capture=True)

    title_scores = {}
    title_chapters = {}

    for line in res.splitlines():
        parts = line.split(",")
        if len(parts) < 4: continue

        try: t_idx = parts[0].split(":")[1]
        except IndexError: continue

        if line.startswith("TINFO:") and parts[1] == "9" and parts[2] == "4":
            title_chapters[t_idx] = int(parts[3].strip('"'))
            title_scores.setdefault(t_idx, 0)

        if line.startswith("SINFO:"):
            if "A_TRUEHD" in line or "TrueHD Atmos" in line:
                title_scores[t_idx] = max(title_scores.get(t_idx, 0), 1000)
                log_callback(f"    [*] Title {t_idx}: Found Lossless Atmos (+1000)")
            elif "A_EAC3" in line and "Atmos" in line:
                title_scores[t_idx] = max(title_scores.get(t_idx, 0), 500)
                log_callback(f"    [*] Title {t_idx}: Found Lossy Atmos (+500)")

    if not title_scores:
        raise RuntimeError("No valid titles found on source.")

    winner = max(title_scores, key=lambda k: (title_scores[k], title_chapters.get(k, 0)))
    winning_score = title_scores[winner]

    if winning_score < 500:
        raise RuntimeError(f"Atmos or Die: Best title ({winner}) has score {winning_score}. No Atmos track found.")

    log_callback(f"[*] Winner: Title {winner} (Score: {winning_score})")
    return winner

def get_metadata_from_musicbrainz(album: str, artist: str, num_tracks: int, max_release_groups: int = 5) -> Tuple[Optional[Dict], Optional[List]]:
    """Fetches track titles and year for CUE sheet generation."""
    try:
        rg_res = mb.search_release_groups(artist=artist, release=album)
        for rg in rg_res.get('release-group-list', [])[:max_release_groups]:
            rel_res = mb.browse_releases(release_group=rg['id'], includes=["recordings"])
            for r in rel_res.get('release-list', []):
                if r.get('status') == 'Official':
                    all_tracks = []
                    for m in r.get('medium-list', []): all_tracks.extend(m.get('track-list', []))
                    if len(all_tracks) == num_tracks:
                        return {'title': rg['title'], 'artist': rg.get('artist-credit-phrase', artist), 'year': r.get('date', 'Unknown')[:4]}, all_tracks
    except Exception: pass
    return None, None

def process_release(src_path: str, artist: str, album: str, album_suffix: str, library_root: str, log_callback: Callable = _no_op_logger) -> None:
    """The unified API entry point for ripping, transcoding, and tagging."""

    full_title = f"{album}{album_suffix}"
    target = Path(library_root) / artist / full_title
    target.mkdir(parents=True, exist_ok=True)

    log_callback(f"[*] Starting job: {artist} - {full_title}")

    # Use manual temp dir creation/deletion to silently suppress WinError 32
    tmp_dir = tempfile.mkdtemp(prefix="carat_")
    tmp_p = Path(tmp_dir)

    try:
        src_p = Path(src_path)

        # --- MakeMKV Rip Logic ---
        if src_p.is_dir() or src_p.suffix.lower() == '.iso':
            source_spec = f"file:{src_path}" if src_p.is_dir() else f"iso:{src_p.resolve()}"
            title_idx = find_primary_title(source_spec, log_callback)

            cmd = [TOOLS.MAKEMKV, "--progress=-stdout", "-r", "mkv", source_spec, title_idx, str(tmp_p), "--minlength=600"]
            run_command(cmd, f"Ripping Title {title_idx}", log_callback)

            mkv_files = list(tmp_p.glob("*.mkv"))
            if not mkv_files: raise RuntimeError("MakeMKV produced no output.")
            working_mkv = max(mkv_files, key=lambda x: x.stat().st_size)
        else:
            working_mkv = src_p.resolve()

        # --- FFprobe & Chapter Extraction ---
        probe_cmd = [TOOLS.FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index,channels,codec_name", "-of", "json", str(working_mkv)]
        res = run_command(probe_cmd, capture=True)
        streams = json.loads(res).get('streams', [])
        candidates = [s for s in streams if s.get('codec_name') == 'truehd']
        if not candidates: raise ValueError("No TrueHD stream found.")
        truehd_idx = int(max(candidates, key=lambda x: int(x.get('channels', 0)))['index'])

        chap_cmd = [TOOLS.FFPROBE, "-v", "quiet", "-print_format", "json", "-show_chapters", str(working_mkv)]
        chaps = json.loads(run_command(chap_cmd, capture=True)).get('chapters', [])

        # --- Metadata & Cover Art ---
        info, tracks = get_metadata_from_musicbrainz(album, artist, len(chaps))
        info = info or {'artist': artist, 'title': album, 'year': 'Unknown'}

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            cover_future = ex.submit(getcoverart.download_cover_art, artist, album, target)

            # --- Porcelain Assembly ---
            m4a_name = f"{full_title}.m4a"
            generate_cue_sheet(target / f"{full_title}.cue", m4a_name, info, chaps, tracks or [])

            # --- Transcode ---
            ff_cmd = [
                TOOLS.FFMPEG, "-hide_banner", "-loglevel", "warning", "-stats",
                "-i", str(working_mkv), "-map", f"0:{truehd_idx}",
                "-metadata", f"title={full_title}", "-c:a", "copy",
                "-f", "mp4", "-movflags", "+faststart", "-strict", "-2",
                "-fflags", "+genpts", "-map_chapters", "-1", "-y", str(target / m4a_name)
            ]
            run_command(ff_cmd, "Finalizing Atmos M4A", log_callback)

            try: cover_future.result(timeout=45)
            except Exception: pass

        log_callback(f"\n[+] Library Entry Complete: {full_title}")

    finally:
        # Silently handles the WinError 32 lock issue
        shutil.rmtree(tmp_dir, ignore_errors=True)

# --- (4) CLI Execution ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Carat: Concise Atmos Ripping Automation Tool (CLI)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("src", help="Path to the source MKV, ISO, or BDMV folder")
    parser.add_argument("artist", help="Name of the artist")
    parser.add_argument("album", help="Base name of the album")
    parser.add_argument("library_root", help="Target library root directory")
    parser.add_argument("--suffix", default=" (Atmos)", help="Suffix appended to album dir/file")

    args = parser.parse_args()

    try:
        process_release(args.src, args.artist, args.album, args.suffix, args.library_root, cli_logger)
    except Exception as e:
        print(f"\n[!] Fatal Error: {e}")
        sys.exit(1)