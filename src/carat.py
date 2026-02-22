import atexit
import os
import signal
import sys, json, subprocess, shutil, tempfile, concurrent.futures, argparse, re, platform
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Callable, Any
import musicbrainzngs as mb
import getcoverart, logger

__all__ = ['rip_album_to_library']

# --- (1) The Porcelain (Metadata & Utils) ---

def seconds_to_cue(seconds: float) -> str:
    """Converts seconds to MM:SS:FF for gapless CUE sheets."""
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}:{int((seconds % 1) * 75):02d}"


def generate_cue_sheet(cue_path: Path, m4a_name: str, info: Dict, chapters: List, mb_tracks: List) -> None:
    """Generates the CUE sheet for gapless playback."""
    with cue_path.open('w', encoding='utf-8') as f:
        f.write(f'PERFORMER "{info["artist"]}"\nTITLE "{info["title"]} (Atmos)"\nREM DATE {info.get("year", "Unknown")}\nFILE "{m4a_name}" WAVE\n')
        for i, ch in enumerate(chapters):
            title = mb_tracks[i]['recording']['title'] if i < len(mb_tracks) else f"Track {i + 1}"
            f.write(f'  TRACK {i + 1:02d} AUDIO\n    TITLE "{title}"\n    INDEX 01 {seconds_to_cue(float(ch["start_time"]))}\n')


def _parse_makemkv_msg(line: str) -> Optional[str]:
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

# --- (2) The Plumbing (Clean Subprocess + Beautifier) ---

def _process_output_line(line: str, output_acc: List[str], state: dict, log_callback: Optional[Callable]):
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
                    if log_callback and 0 <= pct <= 100:
                        log_callback(f"    Atmos Extraction: {pct:.1f}%", is_progress=True)
            except (IndexError, ValueError):
                pass

        state["last_was_progress"] = True
        return

    # [2] ffmpeg Progress
    elif "size=" in line and "time=" in line and "bitrate=" in line:
        if log_callback:
            clean_stats = line.strip().replace("frame=", " ")
            log_callback(f"Transcoding: {clean_stats}", is_progress=True)

        state["last_was_progress"] = True
        return

    # [3] Normal Output
    else:
        msg = _parse_makemkv_msg(line)
        if msg:
            logger.emit(f"[*] {msg}", log_callback)
        elif not any(line.startswith(x) for x in ["DRV:", "TDRV:", "CIDC:", "SINFO:", "TINFO:", "CINFO:"]):
            logger.emit(line, log_callback)

        state["last_was_progress"] = False

def run_command(cmd: List[str], desc: Optional[str] = None, log_callback: Optional[Callable] = None) -> str:
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
    _active_subprocess = process # So we can kill the process from outside this method if it all goes south

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
    # 1. Search backwards through the accumulated log for FFmpeg's final stats
    final_stats = next((line for line in reversed(output_acc) if "size=" in line and "time=" in line), None)

    if final_stats:
        clean_stats = final_stats.strip().replace("frame=", " ")
        summary = f"[+] Transcode finished in {elapsed:.1f}s -> {clean_stats}"
    else:
        summary = f"[+] Task finished in {elapsed:.1f} seconds."

    logger.emit(summary, log_callback)

# --- (3) The Logic (Atmos or Die) ---

def find_primary_title(source_spec: str, log_callback: Optional[Callable] = None) -> str:
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
    def __init__(self) -> None:
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
        self._validate()

    @staticmethod
    def _find(name: str, prospects: Optional[List[str]] = None) -> Optional[str]:
        # noinspection PyDeprecation
        found = shutil.which(name)
        if found: return found

        if prospects is None: prospects = []
        for p in prospects:
            if Path(p).exists(): return str(Path(p))

        return None

    def _validate(self) -> None:
        if any(v is None for v in self.__dict__.values() if not isinstance(v, bool)):
            logger.emit(f"ERROR: Missing dependencies: {[k for k, v in self.__dict__.items() if v is None]}")
            sys.exit(1)


TOOLS = Toolset()
mb.set_useragent("AtmosRipAutomationTool", "0.2", "josh@bloch.us")


def rip_atmos(source_spec: str, mkv_path: Path, title_idx: str = "all",
              log_callback: Optional[Callable] = None) -> Path:
    # Force a strict, absolute Windows path to prevent MakeMKV from mixing slashes
    clean_mkv_path = str(mkv_path.resolve())
    cmd = [TOOLS.MAKEMKV, "--progress=-stdout", "-r", "mkv", source_spec, title_idx, clean_mkv_path, "--minlength=600"]
    run_command(cmd, f"Ripping Title {title_idx}", log_callback)

    mkv_files = list(mkv_path.glob("*.mkv"))
    if not mkv_files: raise RuntimeError("MakeMKV produced no output.")

    winner = max(mkv_files, key=lambda x: x.stat().st_size)
    for f in mkv_files:
        if f != winner: f.unlink()
    return winner


def find_truehd_stream(mkv_path: Path, log_callback: Optional[Callable] = None) -> Optional[int]:
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
                         log_callback: Optional[Callable] = None) -> None:
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


def extract_chapters_from_mkv(mkv_path: Path, log_callback: Optional[Callable] = None) -> List[Dict]:
    cmd = [TOOLS.FFPROBE, "-v", "quiet", "-print_format", "json", "-show_chapters", str(mkv_path)]
    res = run_command(cmd, "Extracting Chapter Markers", log_callback)
    try:
        return json.loads(res).get('chapters', [])
    except json.JSONDecodeError:
        return []

# Maximum number of release groups to search in MusicBrainz when look for the album
MAX_RELEASE_GROUPS: int = 5

def get_metadata_from_musicbrainz(album: str, artist: str, num_tracks: int, log_callback: Optional[Callable] = None) -> Tuple[
    Optional[Dict], Optional[List]]:
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


def merge_iaa_folder(directory_path: Path, ssd_path: Path, log_callback: Optional[Callable] = None) -> Path:
    files = sorted(list(directory_path.glob("*.mk*")))
    if not files: raise FileNotFoundError("No MKV files found in source folder.")
    out = ssd_path / "master.mkv"
    cmd = [TOOLS.MKVMERGE, "--priority", "lower", "-o", str(out)]
    for i, f in enumerate(files):
        cmd.append(str(f) if i == 0 else f"+{str(f)}")

    # Simple blind append logic for IAA
    run_command(cmd, f"Merging IAA Folder", log_callback)
    return out

# We do all of our work in a temp directory, which will contain a huge MKV. The following code ensures that the
# contents of this directory get deleted, come hell or highwater (though they might survive a BSOD or power outage).
TMP_DIR = Path(tempfile.mkdtemp(prefix="carat_"))
_active_subprocess: Optional[subprocess.Popen[str]] = None  # Tracks the currently running tool

def _nuke_tmp_dir():
    """ Terminates active subprocesses and deletes the tmp directory. """
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
        shutil.rmtree(TMP_DIR, ignore_errors=True)

atexit.register(_nuke_tmp_dir)

# Catch OS-level interruptions (Ctrl+C, normal termination signals)
def _signal_handler(signum, frame):
    _nuke_tmp_dir()
    os._exit(1)

for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, _signal_handler)
    except ValueError:
        pass

def rip_album_to_library(src_path: str, artist: str, album: str, library_root: str,
                         log_callback: Optional[Callable] = None) -> None:
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
    # 1. Prepare the destination directory
    target = Path(library_root) / artist / f"{album} (Atmos)"
    target.mkdir(parents=True, exist_ok=True)

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
            atmos_mkv = rip_atmos(source_spec, TMP_DIR, title_idx, log_callback)

        except (ValueError, TypeError):
            # Input is not an integer; handle as Path
            src_p = Path(src_path)

            # --- CASE B: ISO Image ---
            if src_p.suffix.lower() == ".iso":
                source_spec = f"iso:{src_p.resolve()}"
                title_idx = find_primary_title(source_spec, log_callback)
                atmos_mkv = rip_atmos(source_spec, TMP_DIR, title_idx, log_callback)

            # --- CASE C: BDMV Folder Structure (Decrypted Backup) ---
            elif src_p.is_dir() and (src_p / "BDMV").exists():
                source_spec = f"file:{src_p.resolve() / 'BDMV'}"
                title_idx = find_primary_title(source_spec, log_callback)
                atmos_mkv = rip_atmos(source_spec, TMP_DIR, title_idx, log_callback)

            # --- CASE D: IAA / Generic Folder (Merge MKVs) ---
            elif src_p.is_dir():
                atmos_mkv = merge_iaa_folder(src_p, TMP_DIR, log_callback)

            # --- CASE E: Direct MKV File (Bypass Rip) ---
            else:
                atmos_mkv = src_p.resolve()
                if not atmos_mkv.exists(): raise FileNotFoundError(f"Not found: {src_path}")

        # 3. Post-Rip Processing
        # Extract chapter markers from the master MKV (essential for CUE sheet)
        chaps = extract_chapters_from_mkv(atmos_mkv, log_callback)

        # Fetch metadata (Track titles, Year) from MusicBrainz
        info, tracks = get_metadata_from_musicbrainz(album, artist, len(chaps), log_callback)
        info = info or {'artist': artist, 'title': album, 'year': 'Unknown'}

        # 4. Final Assembly (Concurrent)
        # We run the Cover Art download in parallel with the heavy Transcode
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            cover_future = ex.submit(getcoverart.download_cover_art, artist, album, target, log_callback)

            # Generate "Porcelain" (CUE Sheet)
            generate_cue_sheet(target / f"{album} (Atmos).cue", f"{album} (Atmos).m4a", info, chaps, tracks or [])

            # Transcode (TrueHD/Atmos -> M4A Container)
            transcode_mkv_to_m4a(atmos_mkv, target / f"{album} (Atmos).m4a", album, log_callback)

            # Ensure Cover Art finished (soft timeout)
            try:
                cover_future.result(timeout=45)
            except Exception:
                pass
    finally:
        _nuke_tmp_dir()

    logger.emit(f"\n[+] Library Entry Complete: {album}", log_callback)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("artist")
    parser.add_argument("album")
    parser.add_argument("library_root")
    args = parser.parse_args()
    rip_album_to_library(args.source, args.artist, args.album, args.library_root)

if __name__ == "__main__":
    main()
