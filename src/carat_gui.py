"""
Simple GUI for carat (Concise Atmos Ripping Automation Tool).

Provides one-click ripping and remuxing of Dolby Atmos albums in common formats for gapless playback with track
selection. (See carat.py for details.)
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

import itertools
import json
import platform
import queue
import re
import threading
import tkinter as tk
from enum import Enum
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext
from typing import Any

from PIL import Image, ImageTk

import carat
import logger
import music_brainier
from carat import Codec, Container
from music_brainier import MusicBrainzMetadata
from util import _clean_path_arg

CONFIG_FILE = Path.home() / ".carat_config.json"

class OutputProfile(Enum):
    """Output format specification."""
    M4A_LOSSLESS = ("M4A Lossless (TrueHD) [Fire TV Stick / Cube]", Container.M4A, Codec.TRUEHD)
    M4A_LOSSY = ("M4A Lossy Audio (Dolby Digital+) [Apple Devices]", Container.M4A, Codec.EAC3)
    MKV_LOSSLESS = ("MKV Lossless (TrueHD) [Nvidia Shield, PC]", Container.MKV, Codec.TRUEHD)
    FLAC = ("FLAC (Legacy non-Atmos surround) [All Devices]", Container.FLAC, Codec.FLAC)

    def __init__(self, display_name: str, container: Container, codec: Codec):
        self.display_name = display_name
        self.container = container
        self.codec = codec

    @classmethod
    def from_display_string(cls, search_string: str):
        """Reverse lookup to find the enum from the UI string."""
        for profile in cls:
            if profile.display_name == search_string:
                return profile
        # Safe default if the config file has an obsolete string
        return cls.M4A_LOSSLESS


class CaratGUI:
    """ Tkinter GUI for Carat """

    def _log_callback(self, msg: str, is_progress: bool = False) -> None:
        """Thread-safe logging callback from carat to the UI."""
        if is_progress:
            self.status_queue.put(msg)
            if "Extraction:" in msg and "%" in msg:
                try:
                    val = float(msg.split(":")[1].strip().replace("%", ""))
                    self.progress_queue.put(val)
                except ValueError:
                    pass
            elif ("Remuxing:" in msg or "Merging:" in msg or "Slicing:" in msg) and "[" in msg and "%]" in msg:
                try:
                    start = msg.find("[") + 1
                    end = msg.find("%]")
                    val = float(msg[start:end])
                    self.progress_queue.put(val)
                except ValueError:
                    pass
        else:
            self.log_queue.put(msg)
            if "Success: Saved" in msg and "cover to" in msg:
                try:
                    # Extract the path from the end of the log message
                    path_str = msg.split("cover to")[1].strip()
                    self.art_queue.put(path_str)
                except IndexError:
                    pass

    def __init__(self, parent: tk.Tk) -> None:
        self.parent = parent
        self.parent.title("Carat: Concise Atmos Ripping Automation Tool")
        self.parent.geometry("850x850") # Square, like an album cover ;)

        # User our neat logo as this app's icon, unless it's not there, in which case oh well...
        try:
            script_dir = Path(__file__).resolve().parent
            icon_path = str(script_dir / 'assets' / 'carat_logo_icon.png')
            img_icon = tk.PhotoImage(file=icon_path)
            self.parent.iconphoto(True, img_icon)
        except tk.TclError as e:
            logger.emit(f"[!] logo icon missing, falling back to default icon: {e}")

        # Load config first so we can use it in UI init
        self.config = self._load_config()

        # Initialize thread-safe queues
        self.log_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.art_queue = queue.Queue()
        self.done_queue = queue.Queue()

        # Initialize state variables
        self.current_cover_path = None
        self.is_ripping = False
        logger.init(self._log_callback)

        # Initialize variables used by rip-plan revision dialog
        self.action_queue = queue.Queue()
        self.review_event = threading.Event()
        self.review_result = None
        self.review_metadata_var = tk.BooleanVar(value=self.config.get('review_metadata', False))

        # Override window close hook to give the user a chance to back out and clean up if they don't
        self.parent.protocol("WM_DELETE_WINDOW", self._on_close)

        self._init_ui()
        self._start_queue_poller()

    def _on_close(self):
        if self.is_ripping:
            if not messagebox.askyesno("Exit", "Ripping is in progress. Are you sure you want to quit?"):
                return
        # Force cleanup before destroying root
        carat.clean_up()
        self.parent.destroy()

    @staticmethod
    def _load_config() -> dict[Any, Any]:
        """Loads user preferences from the home directory."""
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        return {}

    def _save_config(self) -> None:
        """Saves persistent data to the config file."""
        cfg = {
            "library_root": self.dest_var.get(),
            "output_format": self.output_format_var.get(),
            "review_metadata": self.review_metadata_var.get()
        }
        try:
            CONFIG_FILE.write_text(json.dumps(cfg))
        except (OSError, TypeError):
            pass

    # noinspection PyUnusedLocal
    def _evaluate_button_state(self, *_args: object) -> None:
        """Instantly updates the RIP button based on current inputs and state."""
        # State 3 Guard: If we are actively ripping, ignore typing
        if self.is_ripping:
            return

        # Check if all four fields have text in them
        if not all([self.src_var.get().strip(), self.dest_var.get().strip(),
                    self.artist_var.get().strip(), self.album_var.get().strip()]):
            # State 1: Not Ready
            self.btn_rip.config(state="disabled", text="Fill in Blank Fields (above)")
        else:
            # State 2: Ready (This also acts as the reset for State 4 when a user edits a field)
            self.btn_rip.config(state="normal", text="Rip", command=self._start_rip_thread)

    def _init_ui(self) -> None:
        """Constructs the GUI."""
        style = ttk.Style()
        style.theme_use('clam')

        # Override the disabled button state for high visibility (This includes "Rip Complete")
        style.map('TButton', foreground=[('disabled', 'black')], background=[('disabled', '#e0e0e0')])

        # 1. Destination (Library Root)
        section = itertools.count(1)
        frame_dest = ttk.LabelFrame(self.parent, text=f"{next(section)}. Music Library Root", padding=10)
        frame_dest.pack(fill="x", padx=10, pady=5)

        self.dest_var = tk.StringVar(value=self.config.get("library_root"))
        ttk.Entry(frame_dest, textvariable=self.dest_var).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(frame_dest, text="Browse...", command=self._browse_dest).pack(side="left", padx=(0, 5))

        # --- Format Output Profile Dropdown ---
        self.profiles = [p.display_name for p in OutputProfile]
        saved_profile = self.config.get("output_format", OutputProfile.M4A_LOSSLESS.display_name)
        if saved_profile not in self.profiles:  # Safety check: if saved output format is invalid, revert to default
            saved_profile = OutputProfile.M4A_LOSSLESS.display_name
        self.output_format_var = tk.StringVar(value=saved_profile)

        ttk.Separator(frame_dest, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(frame_dest, text="Format:").pack(side="left", padx=(0, 5))

        self.format_dropdown = ttk.Combobox(
            frame_dest,
            textvariable=self.output_format_var,
            values=self.profiles,
            state="readonly",
            width=48  # Expanded to fit the concise strings perfectly
        )
        self.format_dropdown.pack(side="left")

        # 2. Source Selection
        frame_src = ttk.LabelFrame(self.parent, text=f"{next(section)}. Source (Disc, ISO, or Folder)", padding=10)
        frame_src.pack(fill="x", padx=10, pady=5)

        self.src_var = tk.StringVar()
        ttk.Entry(frame_src, textvariable=self.src_var).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(frame_src, text="Folder/Disc...", command=self._browse_source_folder).pack(side="right", padx=(2, 0))
        ttk.Button(frame_src, text="File...", command=self._browse_source_file).pack(side="right")

        self.src_var.trace_add("write", self._on_source_changed) # Clears old src stuff when new one is entered

        # 3. Metadata & Art Container
        frame_meta_cont = ttk.Frame(self.parent)
        frame_meta_cont.pack(fill="x", padx=10, pady=5)

        # Metadata Sub-Frame
        frame_meta = ttk.LabelFrame(frame_meta_cont, text=f"{next(section)}. Metadata", padding=10)
        frame_meta.pack(side="left", fill="both", expand=True, padx=(0, 5))

        ttk.Label(frame_meta, text="Artist:").grid(row=0, column=0, sticky="w", pady=2)
        self.artist_var = tk.StringVar()
        self.ent_artist = ttk.Entry(frame_meta, textvariable=self.artist_var)
        self.ent_artist.grid(row=0, column=1, sticky="ew", padx=5, pady=2)

        ttk.Label(frame_meta, text="Album:").grid(row=1, column=0, sticky="w", pady=2)
        self.album_var = tk.StringVar()
        self.ent_album = ttk.Entry(frame_meta, textvariable=self.album_var)
        self.ent_album.grid(row=1, column=1, sticky="ew", padx=5, pady=2)

        ttk.Checkbutton(frame_meta, text="Review metadata before ripping", variable=self.review_metadata_var,
                        command=self._save_config).grid(row=99, column=0, columnspan=2, sticky="sw", pady=(15, 0))
        frame_meta.rowconfigure(98, weight=1) # The "Spring" - forces empty row 98 to absorb all leftover vertical space

        # --- Smart Auto-Fill State ---
        self._user_touched_metadata = False
        self._is_autofilling = False

        # Trace the variables so we know if the user manually edits them
        self.artist_var.trace_add("write", self._on_metadata_changed)
        self.album_var.trace_add("write", self._on_metadata_changed)

        frame_meta.columnconfigure(1, weight=1)

        # Cover Art Sub-Frame
        frame_art = ttk.LabelFrame(frame_meta_cont, text="Cover Art", padding=10)
        frame_art.pack(side="right", fill="y", padx=(5, 0))

        art_container = ttk.Frame(frame_art, width=200, height=200)
        art_container.pack()
        art_container.pack_propagate(False)  # Prevents container from shrinking to fit the text

        # The Label inside the container (drop the 'width=15' text sizing)
        self.lbl_art = ttk.Label(art_container, text="Waiting...", anchor="center", background="#eee")
        self.lbl_art.pack(fill="both", expand=True)
        self.lbl_art.bind("<Button-1>", self._change_cover_art)

        # 4. Action Buttons
        frame_actions = ttk.Frame(self.parent)
        frame_actions.pack(fill="x", padx=15, pady=10)

        self.btn_rip = ttk.Button(frame_actions, text="Rip", command=self._start_rip_thread)
        self.btn_rip.pack(side="left", fill="x", expand=True)  # expand=True allows it to stretch

        #5.  Progress & Status
        frame_prog = ttk.Frame(self.parent, padding=(10, 0, 10, 0))
        frame_prog.pack(fill="x", padx=10)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(frame_prog, variable=self.progress_var)
        self.progress_bar.pack(fill="x")
        self.lbl_status = ttk.Label(frame_prog, text="Ready", font=('Segoe UI', 9, 'italic'), foreground="gray")
        self.lbl_status.pack(anchor="w", pady=(2, 0))

        # 6. Console Output
        frame_log = ttk.LabelFrame(self.parent, text="Console Output", padding=10)
        frame_log.pack(fill="both", expand=True, padx=10, pady=10)
        self.console = scrolledtext.ScrolledText(frame_log, state="disabled", font=('Consolas', 9))
        self.console.pack(fill="both", expand=True)

        # Bind the variables to the state evaluator
        self.src_var.trace_add("write", self._evaluate_button_state)
        self.dest_var.trace_add("write", self._evaluate_button_state)
        self.artist_var.trace_add("write", self._evaluate_button_state)
        self.album_var.trace_add("write", self._evaluate_button_state)
        self.output_format_var.trace_add("write", self._evaluate_button_state)

        # Force an initial evaluation on startup
        self._evaluate_button_state()

    @staticmethod
    def _guess_metadata(source_path: str) -> tuple[str, str]:
        """Heuristically guesses (Artist, Album) from the source path."""
        if not source_path:
            return "", ""

        # 0. Strip known media extensions early so they don't leak into the album title
        p = Path(source_path)
        if p.suffix.lower() in ('.iso', '.mkv', '.mp4'):
            name = p.stem
        else:
            name = p.name

        # 1. Windows Drive Root Fallback (e.g., "D:\")
        if (not name or name.endswith(':\\')) and platform.system() == "Windows":
            import ctypes
            volume_buf = ctypes.create_unicode_buffer(1024)
            # noinspection PyUnresolvedReferences
            if ctypes.windll.kernel32.GetVolumeInformationW(str(p), volume_buf, 1024, None, None, None, None, 0):
                name = volume_buf.value

        # 2. The BDMV / Folder / File Fallbacks
        if (name.upper() in ("BDMV", "VIDEO_TS", "CERTIFICATE") or
                (p.is_file() and " - " not in name and " - " in p.parent.name)):
            name = p.parent.name

        # 3. Strip bracketed and parenthetical cruft (e.g., [FLAC], (2023 Mix))
        clean_name = re.sub(r'[(\[].*?[)\]]', '', name)

        # 4. Strip standalone audiophile tags that escaped brackets
        clean_name = re.sub(r'\b(ATMOS|5\.1|7\.1|WEB|OF|TR24|TR16)\b', '', clean_name, flags=re.IGNORECASE)

        # 5. Split strictly on " - " (spaces around dash protect hyphenated words like "3-D")
        parts = [part.strip() for part in clean_name.split(" - ") if part.strip()]

        if len(parts) >= 2:
            artist = parts[0]
            album = parts[1]
            # Any trailing years (e.g., parts[2]) are naturally ignored!
            return artist, album
        else:
            # Fallback for no " - " delimiter (e.g., Steely_Dan_Gaucho)
            # Replace underscores with spaces, collapse multiple spaces, and capitalize
            clean_name = clean_name.replace("_", " ")
            album = re.sub(r'\s+', ' ', clean_name).strip().title()
            return "", album

    def _apply_autofill(self, path: str) -> None:
        """Applies the heuristic guesser and intelligently overwrites the UI."""
        artist_guess, album_guess = self._guess_metadata(path)

        can_overwrite = (not self._user_touched_metadata) or \
                        (not self.artist_var.get().strip() and not self.album_var.get().strip())

        if can_overwrite:
            self._is_autofilling = True

            # Unconditionally replace UI state (clearing it if the guess is empty)
            self.artist_var.set(artist_guess)
            self.album_var.set(album_guess)

            self._is_autofilling = False
            self._user_touched_metadata = False

    def _browse_source_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Media File", "*.iso *.mkv *.mp4")])
        if path:
            self.src_var.set(path)
            self._user_touched_metadata = False  # Reset the manual edit lock for the new file
            self._apply_autofill(path)

    def _browse_source_folder(self) -> None:
        path = filedialog.askdirectory(title="Folder of media files or Blu-ray Drive")
        if path:
            self.src_var.set(path)
            self._user_touched_metadata = False  # Reset the manual edit lock for the new folder
            self._apply_autofill(path)

    # noinspection PyUnusedLocal
    def _on_metadata_changed(self, *_args) -> None:
        """Marks the metadata as manually edited if the user types in the fields."""
        if not getattr(self, '_is_autofilling', False):
            self._user_touched_metadata = True

    def _on_source_changed(self, *_args):
        """Clears stale console output and cover art when the source is modified."""

        # Clear the console and art only if there is a stale rip log present
        current_console_text = self.console.get(1.0, tk.END)
        if "=== Carat Rip Log ===" in current_console_text:
            self._clear_console()
            self.clear_cover_art()

    def _browse_dest(self) -> None:
        """Prompts the user for the library root."""
        path = filedialog.askdirectory(title="Select Library Root")
        if path: self.dest_var.set(path)

    def _start_rip_thread(self) -> None:
        """Collects inputs and launches the background workers."""
        # Auto-clear the console and cover art on new rip
        self._clear_console()
        self.clear_cover_art()

        # Change state to State #3 - Rip in progress
        self._save_config()
        self.is_ripping = True
        self.btn_rip.config(state="disabled", text="Ripping in Progress...")

        self.progress_bar.config(mode='indeterminate')
        self.progress_bar.start(50)  # 50 ms. refresh interval

        # Fire and forget!
        thread = threading.Thread(target=self._run_rip_thread)
        thread.daemon = True
        thread.start()

    def clear_cover_art(self):
        """ Clear the cover art from the GUI (typically because a new rip is being specified) """
        self.lbl_art.configure(image='', text="Waiting...")
        self.lbl_art.image = None  # type: ignore
        self.lbl_art.update_idletasks()

    def _run_rip_thread(self) -> None:
        try:
            # Gather UI Variables safely
            source_path = _clean_path_arg(self.src_var.get())
            artist = self.artist_var.get().strip()
            album = self.album_var.get().strip()
            lib_root = _clean_path_arg(self.dest_var.get())
            profile = OutputProfile.from_display_string(self.output_format_var.get())
            output_container = profile.container
            preferred_codec = profile.codec

            # Phase 1: prepare rip plan
            plan = carat.prepare_album_rip(source_path, artist, album, lib_root, output_container, preferred_codec)

            # Phase 2: (optional): inspect and possibly revise rip plan
            if self.review_metadata_var.get():
                logger.emit("\n[*] Awaiting user confirmation of metadata...")
                self.action_queue.put(("REVIEW", plan))
                self.review_event.wait()
                self.review_event.clear()

                if self.review_result is None:
                    logger.emit("[-] Rip cancelled by user during review.")
                    self.done_queue.put(("FAIL", None, None))
                    return

                logger.emit("[+] Re-calculating Rip Plan based on user overrides...\n")
                plan = plan.revise(self.review_result.get('title_idx'), self.review_result.get('stream_idx'),
                    new_mbid=self.review_result.get('mbid'), new_medium_idx=self.review_result.get('medium'),
                    new_artist=self.review_result.get('artist'), new_album=self.review_result.get('album'),
                    start_new_log=False)  # <-- Stops the Phase 3 log from clobbering Phase 1 log!

            # Phase 2.5: Smart Overwrite Protection
            plan = self._enforce_overwrite_protection(plan)
            if not plan:
                self.done_queue.put(("FAIL", None, None))
                return

            # Phase 3: Execute rip plan
            success = carat.execute_album_rip(plan)

            if success:
                self.done_queue.put(("SUCCESS", plan))
            else:
                self.done_queue.put(("FAIL", plan))
        except Exception as e:
            logger.emit(f"\n[!] Carat Engine Error: {str(e)}")
            self.done_queue.put(("FAIL", None, None))

    def _enforce_overwrite_protection(self, plan: carat.RipPlan) -> carat.RipPlan | None:
        """
        Thread-safe helper to check for directory collisions and prompt the user.
        Returns the safe RipPlan, or None if the user canceled.
        """
        if not plan.target_dir.exists():
            return plan

        logger.emit("\n[!] Target directory exists. Awaiting user conflict resolution...")
        self.action_queue.put(("SUFFIX", plan))
        self.review_event.wait()
        self.review_event.clear()

        action = self.review_result.get("action") if self.review_result else "cancel"

        if action == "cancel":
            logger.emit("[-] Rip cancelled by user to prevent overwrite.")
            return None
        elif action == "overwrite":
            logger.emit("[!] User elected to OVERWRITE existing directory.")
            return plan
        elif action == "suffix":
            safe_plan = plan.with_suffix(self.review_result["value"])
            logger.emit(f"[+] Applied custom suffix for new mix: {safe_plan.audio_profile.suffix}")
            return safe_plan

        return None

    def _trigger_post_rip_revise(self, plan) -> None:
        dialog = ReviseDialog(self.parent, plan)
        self.parent.wait_window(dialog)

        if dialog.result:
            self._clear_console()
            self.btn_rip.config(state="disabled", text="Processing Revised Plan...")
            self.lbl_status.config(text="Initializing...")
            self.btn_rip.config(state="disabled", text="Re-ripping using new metadata...")
            self.progress_bar.config(mode="indeterminate")
            self.progress_bar.start(10)
            self.is_ripping = True
            threading.Thread(target=self._run_revise_thread, args=(plan, dialog.result), daemon=True).start()

    def _run_revise_thread(self, old_plan, review_result) -> None:
        try:
            logger.emit("[*] Post-Rip override initiated by user. Re-calculating plan...\n")
            new_plan = old_plan.revise(review_result.get('title_idx'), new_stream_idx=review_result.get('stream_idx'),
                new_mbid=review_result.get('mbid'), new_medium_idx=review_result.get('medium'),
                new_artist=review_result.get('artist'), new_album=review_result.get('album'))

            if new_plan.matched_candidate and 'cover_art_path' in new_plan.matched_candidate:
                self.art_queue.put(new_plan.matched_candidate['cover_art_path'])

            safe_plan = self._enforce_overwrite_protection(new_plan)
            if not safe_plan:
                self.done_queue.put(("FAIL", old_plan))
                return

            success = carat.execute_album_rip(safe_plan)
            if success:
                self.done_queue.put(("SUCCESS", safe_plan))
            else:
                self.done_queue.put(("FAIL", safe_plan))
        except Exception as e:
            logger.emit(f"\n[!] Carat Engine Error during revise: {str(e)}")
            self.done_queue.put(("FAIL", old_plan))

    def _finalize_ui(self, success: bool, got_metadata: bool) -> None:
        self.progress_bar.stop()
        self.progress_bar.config(mode='determinate')

        self.is_ripping = False

        if success:
            self.progress_var.set(100)
            self.lbl_status.config(text="Idle")
            self.btn_rip.config(state="disabled", text="Rip Complete")  # State 4: Complete
            if not got_metadata:
                messagebox.showwarning(
                    "Warning",
                    "The rip completed successfully, but Carat was unable to fetch the tracklist metadata from MusicBrainz.\n\n"
                    "Your files have been saved as 'Track 1', 'Track 2', etc., and you may need to edit the tags and CUE sheet manually."
                )
        else:
            self.lbl_status.config(text="Failed")
            self.btn_rip.config(state="disabled", text="Rip Failed!")  # State 5: Failed

    def _display_cover(self, path: Path) -> None:
        """Updates the cover art label using Pillow."""
        self.current_cover_path = path
        try:
            pil_img = Image.open(path)
            pil_img.thumbnail((200, 200))
            img = ImageTk.PhotoImage(pil_img)
            self.lbl_art.config(image=img, text="")
            self.lbl_art.image = img  # type: ignore
        except (OSError, tk.TclError):
            self.lbl_art.config(text="[Image Error]", image="")

    # noinspection PyUnusedLocal
    def _change_cover_art(self, _event: object) -> None:
        """Allows user to click and replace the cover art manually."""
        if not self.current_cover_path: return
        new_path = filedialog.askopenfilename(filetypes=[("Images", "*.jpg *.png")])
        if new_path:
            try:
                img = Image.open(new_path)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(self.current_cover_path, "JPEG", quality=95)
                self._display_cover(self.current_cover_path)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to update cover: {e}")

    def _show_review_dialog(self, plan) -> None:
        """Isolated handler for the Revise Dialog to prevent blocking the poller."""
        dialog = ReviseDialog(self.parent, plan)
        self.parent.wait_window(dialog)
        self.review_result = dialog.result
        self.review_event.set()

    def _show_suffix_dialog(self, plan) -> None:
        """Isolated handler for the Suffix Dialog to prevent blocking the poller."""
        dialog = SuffixDialog(self.parent, plan.target_dir, plan.audio_profile.suffix)
        self.parent.wait_window(dialog)
        self.review_result = dialog.result
        self.review_event.set()

    def _start_queue_poller(self) -> None:
        """Consumes queue events and updates the UI (must run on the main thread)."""
        # Bulk log insertion for responsiveness
        if not self.log_queue.empty():
            self.console.config(state="normal")
            logs = []
            while not self.log_queue.empty():
                try:
                    logs.append(self.log_queue.get_nowait())
                except queue.Empty:
                    break
            if logs:
                self.console.insert("end", "\n".join(logs) + "\n")
                self.console.see("end")
            self.console.config(state="disabled")

        # Check for UI Intercepts
        while not self.action_queue.empty():
            msg = self.action_queue.get_nowait()
            if isinstance(msg, tuple) and msg[0] == "REVIEW":
                self.parent.after(0, self._show_review_dialog, msg[1])
            elif isinstance(msg, tuple) and msg[0] == "SUFFIX":
                self.parent.after(0, self._show_suffix_dialog, msg[1])

        # --- Check for Rip Completion ---
        while not self.done_queue.empty():
            status = self.done_queue.get_nowait()

            # Stop the progress bar and reset state
            self.progress_bar.stop()
            self.progress_bar.config(mode='determinate')
            self.is_ripping = False
            self.progress_var.set(100)

            if isinstance(status, tuple):
                msg_type = status[0]
                # Safely extract the plan if it was passed back
                plan: carat.RipPlan | None = status[1] if len(status) > 1 else None

                if msg_type == "SUCCESS" and plan is not None:
                    self.lbl_status.config(text="Idle")
                    self.btn_rip.config(
                        state="normal",
                        text="Rip Complete — Rip Additional Stream",
                        command=lambda p=plan: self._trigger_post_rip_revise(p)
                    )
                elif msg_type == "FAIL":
                    self.lbl_status.config(text="Failed / Cancelled")
                    if plan:
                        # We have a plan! Guide them to the Revise flow to fix the collision.
                        self.btn_rip.config(
                            state="normal",
                            text="Revise Plan to Retry",
                            command=lambda p=plan: self._trigger_post_rip_revise(p)
                        )
                    else:
                        # Fatal error before a plan was created. Reset to square one.
                        self.btn_rip.config(
                            state="normal",
                            text="Rip Failed! (Check Log)",
                            command=self._start_rip_thread
                        )

        while not self.progress_queue.empty():
            # If we get a valid number, we are Determinate. Stop any bouncing.
            if self.progress_bar.cget("mode") != "determinate":
                self.progress_bar.stop()
                self.progress_bar.config(mode='determinate')

            self.progress_var.set(self.progress_queue.get_nowait())

        while not self.status_queue.empty():
            msg = self.status_queue.get_nowait()
            if not self.is_ripping:  # Ignore delayed messages if the rip is already finished
                continue
            self.lbl_status.config(text=msg)

            # ONLY switch to indeterminate (bounce) if we are processing WITHOUT a percentage
            if ("Remuxing" in msg or "Slicing" in msg) and "%" not in msg and self.progress_bar.cget(
                    "mode") != "indeterminate":
                self.progress_bar.config(mode="indeterminate")
                self.progress_bar.start(50)  # 50 ms. refresh interval
                self.btn_rip.config(text="Processing...")

            # If we DO have a percentage (bracket style), ensure we stay Determinate
            # --- PATCH: Catch Remuxing, Merging, and Slicing ---
            elif ("Remuxing" in msg or "Merging" in msg or "Slicing" in msg) and "[" in msg and "%]" in msg:
                if self.progress_bar.cget("mode") != "determinate":
                    self.progress_bar.stop()
                    self.progress_bar.config(mode="determinate")

                if "Merging" in msg:
                    self.btn_rip.config(text="Merging Audio Files...")
                elif "Slicing" in msg:
                    self.btn_rip.config(text="Slicing Tracks...")
                else:
                    self.btn_rip.config(text="Remuxing...")

        while not self.art_queue.empty():
            self._display_cover(self.art_queue.get_nowait())

        # noinspection PyTypeChecker
        self.parent.after(100, self._start_queue_poller)

    def _clear_console(self) -> None:
        """Wipes the console text box clean."""
        self.console.config(state="normal")
        self.console.delete('1.0', tk.END)
        self.console.config(state="disabled")

    def handle_fatal_error(self, message: str) -> None:
        """
        Matches the Callable[[str], None] signature.
        Pops the dialog over the main window, then cleanly kills the app.
        """
        from tkinter import messagebox
        import sys

        messagebox.showerror("Carat: Startup Error", message, parent=self.parent)
        self.parent.destroy()
        sys.exit(1)


class ReviseDialog(tk.Toplevel):
    """ Modal Dialog that allows the user to override heuristic rip plans. """

    def __init__(self, parent, plan):
        super().__init__(parent)
        self.title("Inspect or Revise Rip Plan")
        self.geometry("850x650")
        self.transient(parent)
        self.grab_set()

        # 1. Initialize Core State
        self.result = None
        self.plan = plan  # Saved for validation helper
        self.current_mb_tracks = 0
        self.current_mb_duration = 0

        if plan.matched_candidate and 'tracks' in plan.matched_candidate:
            self.current_mb_tracks = len(plan.matched_candidate['tracks'])
            self.current_mb_duration = sum((int(t.get('duration_ms') or 0)) for t in plan.matched_candidate['tracks'])

        # 2. Build UI Regions
        self._build_top_frame()

        # Master container for the side-by-side panes
        mid_frame = ttk.Frame(self, padding=10)
        mid_frame.pack(fill="both", expand=True)
        mid_frame.columnconfigure(0, weight=2, uniform="pane_split")  # 40% Left
        mid_frame.columnconfigure(1, weight=3, uniform="pane_split")  # 60% Right
        mid_frame.rowconfigure(0, weight=1)

        self._build_left_pane(mid_frame)
        self._build_right_pane(mid_frame)
        self._build_bottom_frame()

        # 3. Run initial validation pass
        self._update_validation_status()

    def _build_top_frame(self):
        """Constructs the MusicBrainz ID search and medium selection bar."""
        top_frame = ttk.Frame(self, padding=10)
        top_frame.pack(fill="x")

        current_mbid = self.plan.matched_candidate['mbid'] if self.plan.matched_candidate else ""
        current_medium = self.plan.matched_candidate.get('medium_index', 1) if self.plan.matched_candidate else 1

        ttk.Label(top_frame, text="MusicBrainz ID:").grid(row=0, column=0, sticky="e")
        self.mbid_var = tk.StringVar(value=current_mbid)
        self.mbid_var.trace_add("write", self._on_mbid_changed)
        ttk.Entry(top_frame, textvariable=self.mbid_var, width=45).grid(row=0, column=1, padx=5, sticky="w")

        ttk.Label(top_frame, text="Medium:").grid(row=1, column=0, sticky="e", pady=5)
        self.medium_var = tk.IntVar(value=current_medium)
        self.medium_var.trace_add("write", self._trigger_live_preview)
        ttk.Spinbox(top_frame, from_=1, to=99, textvariable=self.medium_var, width=5).grid(row=1, column=1, sticky="w",
                                                                                           padx=5, pady=5)

        import webbrowser
        link_lbl = ttk.Label(top_frame, text="View Release on MusicBrainz", foreground="blue", cursor="hand2")
        link_lbl.grid(row=2, column=1, sticky="w", padx=5)
        link_lbl.bind("<Button-1>", lambda e: webbrowser.open(f"https://musicbrainz.org/release/{self.mbid_var.get()}"))

    def _build_left_pane(self, parent_container):
        """Constructs the local naming overrides and the MusicBrainz live preview tree."""
        left_frame = ttk.LabelFrame(parent_container, text="Current MusicBrainz Tracklist")
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        override_frame = ttk.LabelFrame(left_frame, text=" Local Naming Overrides (Does not trigger metadata search) ")
        override_frame.pack(fill="x", padx=5, pady=(0, 10))

        ttk.Label(override_frame, text="Override Artist:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.artist_var = tk.StringVar(value=self.plan.artist)
        ttk.Entry(override_frame, textvariable=self.artist_var, width=35).grid(row=0, column=1, padx=5, pady=5,
                                                                               sticky="w")

        ttk.Label(override_frame, text="Override Album:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.album_var = tk.StringVar(value=self.plan.album)
        ttk.Entry(override_frame, textvariable=self.album_var, width=35).grid(row=1, column=1, padx=5, pady=5,
                                                                              sticky="w")

        if self.plan.matched_candidate:
            mb_artist = self.plan.matched_candidate.get('artist', self.plan.artist)
            mb_album = self.plan.matched_candidate.get('title', self.plan.album)
            total_mins, total_secs = divmod(self.current_mb_duration // 1000, 60)
            initial_text = f"{mb_artist} - {mb_album} ({self.current_mb_tracks} tracks, {total_mins}:{total_secs:02d})"
            self.lbl_preview_status = ttk.Label(left_frame, text=initial_text, font=("Segoe UI", 10, "bold"))
        else:
            self.lbl_preview_status = ttk.Label(left_frame, text="Matched Release: None", foreground="gray")

        self.lbl_preview_status.pack(anchor="w", pady=(5, 0), padx=5)

        self.mb_tree = ttk.Treeview(left_frame, columns=("Length",), show="tree headings", height=12)
        self.mb_tree.heading("#0", text="Track Title")
        self.mb_tree.heading("Length", text="Length")
        self.mb_tree.column("#0", width=250)
        self.mb_tree.column("Length", width=60, anchor="e", stretch=False)
        self.mb_tree.pack(fill="both", expand=True, padx=5, pady=5)

        if self.plan.matched_candidate and 'tracks' in self.plan.matched_candidate:
            for i, track in enumerate(self.plan.matched_candidate['tracks']):
                duration_ms = int(track.get('duration_ms') or 0)
                mins, secs = divmod(duration_ms // 1000, 60)
                title = track.get('title', 'Unknown')
                self.mb_tree.insert("", "end", text=f"{i + 1}. {title}", values=(f"{mins}:{secs:02d}",))

        def _prevent_mb_selection(_event):
            sel = self.mb_tree.selection()
            if sel: self.mb_tree.selection_remove(sel)

        self.mb_tree.bind("<<TreeviewSelect>>", _prevent_mb_selection)

    def _build_right_pane(self, parent_container):
        """Constructs the Disc Topology tree and populates it with MakeMKV state."""
        right_frame = ttk.LabelFrame(parent_container, text="Disc Topology Override")
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        self.topo_tree = None
        if self.plan.disc_topology is not None:
            self.topo_tree = ttk.Treeview(right_frame, show="tree")
            self.topo_tree.tag_configure('title', font=("TkDefaultFont", 9, "bold"))
            self.topo_tree.tag_configure('atmos', font=("TkDefaultFont", 9, "bold"), foreground="blue")
            self.topo_tree.pack(fill="both", expand=True, padx=5, pady=5)

            for t_idx, title_info in self.plan.disc_topology.items():
                dur_str = f"{title_info.duration_sec // 60}:{title_info.duration_sec % 60:02d}"
                t_text = f"Title {t_idx} ({title_info.num_chapters} tracks, {dur_str})"

                file_name = getattr(title_info, 'file_name', '')
                if file_name and file_name.lower() != "unknown":
                    t_text += f" — {file_name}"

                t_node = self.topo_tree.insert("", "end", iid=t_idx, text=t_text, tags=('title',), open=True)

                for s_idx, stream in title_info.streams.items():
                    if any(v in stream.lower() for v in ["mpeg", "avc", "hevc"]):
                        continue

                    s_text = f"Stream {s_idx}: {stream}"
                    tags = ('atmos',) if s_idx in title_info.atmos_streams else ()
                    s_node = self.topo_tree.insert(t_node, "end", iid=f"{t_idx}|{s_idx}", text=s_text, tags=tags)

                    if self.plan.title_idx == t_idx and self.plan.audio_profile and str(
                            self.plan.audio_profile.stream_idx) == str(s_idx):
                        self.topo_tree.selection_set(s_node)

            def _enforce_stream_selection(_event):
                sel = self.topo_tree.selection()
                if sel and "|" not in sel[0]:
                    self.topo_tree.selection_remove(sel[0])
                self.after(10, self._update_validation_status)

            self.topo_tree.bind("<<TreeviewSelect>>", _enforce_stream_selection)
        else:
            ttk.Label(right_frame, text="No Disc Topology Available", justify="center").pack(expand=True)

    def _build_bottom_frame(self):
        """Constructs the validation status text and action buttons."""
        self.val_frame = ttk.Frame(self, padding=(10, 0))
        self.val_frame.pack(fill="x", pady=(0, 5))

        self.lbl_track_match = ttk.Label(self.val_frame, text="Waiting for selection...",
                                         font=("TkDefaultFont", 12, "bold"))
        self.lbl_track_match.pack(side="left", padx=(0, 40))
        self.lbl_dur_match = ttk.Label(self.val_frame, font=("TkDefaultFont", 12, "bold"))
        self.lbl_dur_match.pack(side="left")

        btn_frame = ttk.Frame(self, padding=10)
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="right", padx=5)

        self.btn_rip = ttk.Button(btn_frame, text="Rip", command=self._on_rip)
        self.btn_rip.pack(side="right")

    def _update_validation_status(self):
        """Compares the selected MKV Title against the Live MusicBrainz preview."""
        if not hasattr(self, 'current_mb_tracks') or not self.topo_tree:
            return

        sel = self.topo_tree.selection()
        if not sel:
            self.lbl_track_match.config(text="No Stream Selected", foreground="gray")
            self.lbl_dur_match.config(text="")
            if hasattr(self, 'btn_rip'):
                self.btn_rip.state(['disabled'])  # Disable if nothing selected
            return

        iid = sel[0]
        t_idx = iid.split('|')[0] if '|' in iid else iid

        title_info = self.plan.disc_topology.get(t_idx)
        if not title_info:
            return

        mkv_tracks = title_info.num_chapters
        mkv_dur = title_info.duration_sec * 1000

        mb_tracks = self.current_mb_tracks
        mb_dur = self.current_mb_duration

        # 1. Track Count Validation (THE FATAL GUARDRAIL)
        if mkv_tracks == mb_tracks:
            self.lbl_track_match.config(text=f"✔ Tracks Match ({mkv_tracks})", foreground="green")
            if hasattr(self, 'btn_rip'):
                self.btn_rip.state(['!disabled'])  # ENABLE BUTTON
        else:
            self.lbl_track_match.config(text=f"✖ Track Mismatch (MKV: {mkv_tracks} / MB: {mb_tracks})",
                                        foreground="red")
            if hasattr(self, 'btn_rip'):
                self.btn_rip.state(['disabled'])  # DISABLE BUTTON

        # 2. Duration Validation
        if mb_dur == 0 or mkv_dur == 0:
            self.lbl_dur_match.config(text="? Duration Unknown", foreground="gray")
        else:
            diff = abs(mkv_dur - mb_dur) / float(max(mkv_dur, mb_dur))
            if diff <= 0.10:  # 10% tolerance
                self.lbl_dur_match.config(text="✔ Duration Match (<10% variance)", foreground="green")
            else:
                self.lbl_dur_match.config(text="✖ Duration Mismatch (>10% variance)", foreground="red")

    def _on_mbid_changed(self, *_args):
        """Resets the medium index to 1 when a new MBID is entered, and triggers a preview."""
        self.medium_var.set(1)
        self._trigger_live_preview()

    def _trigger_live_preview(self, *_args):
        if hasattr(self, '_preview_timer') and self._preview_timer:
            self.after_cancel(self._preview_timer)
        self._preview_timer = self.after(750, self._fetch_live_preview)

    def _fetch_live_preview(self):
        mbid = self.mbid_var.get().strip()
        medium = self.medium_var.get()

        if len(mbid) != 36:
            self.lbl_preview_status.config(text="Waiting for valid 36-character MBID...", foreground="gray")
            return

        self.lbl_preview_status.config(text="Fetching metadata...", foreground="blue")

        def worker():
            """ Daemon thread to fetch the MB preview. """
            try:
                metadata = music_brainier.fetch_metadata(mbid, medium)
                self.after(0, lambda: self._update_preview_ui(metadata))
            except Exception as e:
                self.after(0, lambda: self.lbl_preview_status.config(text=f"Fetch failed: {e}", foreground="red"))

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _update_preview_ui(self, metadata: MusicBrainzMetadata):
        if not self.winfo_exists():
            return

        self.mb_tree.delete(*self.mb_tree.get_children())

        if not metadata:
            self.lbl_preview_status.config(text=f"Error: Medium not found in this release.", foreground="red")
            return

        artist = metadata['artist']
        album = metadata['title']
        tracks = metadata['tracks']

        total_ms = 0
        for i, track in enumerate(tracks):
            duration_ms = track['duration_ms']
            total_ms += duration_ms
            mins, secs = divmod(duration_ms // 1000, 60)
            title = track['title']
            self.mb_tree.insert("", "end", text=f"{i + 1}. {title}", values=(f"{mins}:{secs:02d}",))

        total_mins, total_secs = divmod(total_ms // 1000, 60)
        status_text = f"{artist} - {album} ({len(tracks)} tracks, {total_mins}:{total_secs:02d})"
        self.lbl_preview_status.config(text=status_text, font=("Segoe UI", 10, "bold"))

        # Update the state variables and trigger the validation check
        self.artist_var.set(artist)
        self.album_var.set(album)
        self.current_mb_tracks = len(tracks)
        self.current_mb_duration = total_ms
        self._update_validation_status()

    def _on_rip(self):
        new_mbid = self.mbid_var.get().strip()
        if len(new_mbid) > 0 and len(new_mbid) != 36:
            from tkinter import messagebox
            messagebox.showerror("Invalid Input", "MBID must be a 36-character UUID, or empty.", parent=self)
            return

        selected_title: str | None = None
        selected_stream: int | None = None

        if self.topo_tree:
            sel = self.topo_tree.selection()
            if sel:
                iid = sel[0]
                if "|" in iid:
                    t_idx, s_idx = iid.split("|")
                    selected_title = t_idx
                    selected_stream = int(s_idx)
                else:
                    selected_title = iid

        self.result = {
            "mbid": new_mbid,
            "medium": self.medium_var.get(),
            "title_idx": selected_title,
            "stream_idx": selected_stream,
            'artist': self.artist_var.get().strip(),
            'album': self.album_var.get().strip()
        }
        self.destroy()

class SuffixDialog(tk.Toplevel):
    """Modal Dialog to resolve directory overwrite conflicts."""
    def __init__(self, parent, folder_name: str, current_suffix: str = ""):
        super().__init__(parent)
        self.title("Folder Already Exists")
        self.geometry("450x170")
        self.transient(parent)
        self.grab_set()
        self.result = None

        ttk.Label(self, text=f"The folder '{folder_name}' already exists.", font=("Segoe UI", 10, "bold")).pack(
            pady=(15, 5))
        ttk.Label(self, text="Enter a suffix to save as a new mix (e.g., 'Instrumental'), or Overwrite:").pack(
            pady=(0, 10))

        # --- THE NUDGE LOGIC ---
        clean_suffix = current_suffix.strip()
        if clean_suffix:
            suggested_text = f"{clean_suffix} (Alt Mix)"
        else:
            suggested_text = "(Alt Mix)"

        self.suffix_var = tk.StringVar(value=suggested_text)

        # Highlight just the words "Alt Mix" so typing instantly replaces it!
        entry = ttk.Entry(self, textvariable=self.suffix_var, width=30)
        entry.pack(pady=5)

        start_idx = len(suggested_text) - 8  # Highlights the "Alt Mix" part
        entry.select_range(start_idx, start_idx + 7)
        entry.focus()

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Cancel Rip", command=self._on_cancel).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Overwrite Original", command=self._on_overwrite).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Save as New Mix", command=self._on_save).pack(side="left", padx=5)

    def _on_cancel(self):
        self.result = {"action": "cancel"}
        self.destroy()

    def _on_overwrite(self):
        self.result = {"action": "overwrite"}
        self.destroy()

    def _on_save(self):
        suffix = self.suffix_var.get().strip()
        if suffix:
            self.result = {"action": "suffix", "value": suffix}
        else:
            self.result = {"action": "overwrite"}  # Fallback if they leave it blank
        self.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = CaratGUI(root)
    carat.init(app.handle_fatal_error)
    root.mainloop()
