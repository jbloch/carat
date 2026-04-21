"""
Simple GUI for carat (Concise Atmos Ripping Automation Tool).

Provides one-click ripping and remuxing of Dolby Atmos albums in common formats for gapless playback with track
selection. (See carat.py for details.)
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

__author__ = "Joshua Bloch"
__copyright__ = "Copyright 2026, Joshua Bloch"
__license__ = "MIT"
__version__ = "1.0B2.1"

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

CONFIG_FILE = Path.home() / ".carat_config.json"

class OutputProfile(Enum):
    """Output format specification."""
    M4A_LOSSLESS = ("M4A Lossless (TrueHD) [Fire TV Stick / Cube]", ".m4a", "truehd")
    M4A_LOSSY = ("M4A Lossy Audio (Dolby Digital+) [Apple Devices]", ".m4a", "eac3")
    MKV_LOSSLESS = ("MKV Lossless (TrueHD) [Nvidia Shield, PC]", ".mkv", "truehd")

    def __init__(self, display_name: str, container: str, codec: str):
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
            elif ("Remuxing:" in msg or "Merging:" in msg) and "[" in msg and "%]" in msg:
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
            self.parent.iconphoto(False, img_icon)
        except tk.TclError as e:
            logger.emit(f"[!] logo icon missing, falling back to default icon: {e}")

        # Load config first so we can use it in UI init
        self.config = self._load_config()

        # Initialize thread-safe queues
        self.log_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.art_queue = queue.Queue()

        # Initialize state variables
        self.current_cover_path = None
        self.is_ripping = False
        logger.init(self._log_callback)

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
    def _load_config() -> dict[Any, Any] | None:
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
            "output_format": self.output_format_var.get()
        }
        try:
            CONFIG_FILE.write_text(json.dumps(cfg))
        except (OSError, TypeError):
            pass

    # noinspection PyUnusedLocal
    def _evaluate_button_state(self, *args: object) -> None:
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
            self.btn_rip.config(state="normal", text="Rip Atmos")

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

        # 3. Metadata & Art Container
        frame_meta_cont = ttk.Frame(self.parent)
        frame_meta_cont.pack(fill="x", padx=10, pady=5)

        # Metadata Sub-Frame
        frame_meta = ttk.LabelFrame(frame_meta_cont, text=f"{next(section)} Metadata", padding=10)
        frame_meta.pack(side="left", fill="both", expand=True, padx=(0, 5))

        ttk.Label(frame_meta, text="Artist:").grid(row=0, column=0, sticky="w", pady=2)
        self.artist_var = tk.StringVar()
        self.ent_artist = ttk.Entry(frame_meta, textvariable=self.artist_var)
        self.ent_artist.grid(row=0, column=1, sticky="ew", padx=5, pady=2)

        ttk.Label(frame_meta, text="Album:").grid(row=1, column=0, sticky="w", pady=2)
        self.album_var = tk.StringVar()
        self.ent_album = ttk.Entry(frame_meta, textvariable=self.album_var)
        self.ent_album.grid(row=1, column=1, sticky="ew", padx=5, pady=2)

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

        self.btn_rip = ttk.Button(frame_actions, text="RIP ATMOS", command=self._start_rip_thread)
        self.btn_rip.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.btn_clear = ttk.Button(frame_actions, text="Clear Console", command=self._clear_console)
        self.btn_clear.pack(side="right")

        # Progress & Status
        frame_prog = ttk.Frame(self.parent, padding=(10, 0, 10, 0))
        frame_prog.pack(fill="x", padx=10)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(frame_prog, variable=self.progress_var)
        self.progress_bar.pack(fill="x")
        self.lbl_status = ttk.Label(frame_prog, text="Ready", font=('Segoe UI', 9, 'italic'), foreground="gray")
        self.lbl_status.pack(anchor="w", pady=(2, 0))

        # Console Output
        frame_log = ttk.LabelFrame(self.parent, text="Console Output", padding=10)
        frame_log.pack(fill="both", expand=True, padx=10, pady=10)
        self.txt_log = scrolledtext.ScrolledText(frame_log, state="disabled", font=('Consolas', 9))
        self.txt_log.pack(fill="both", expand=True)

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
    def _on_metadata_changed(self, *args) -> None:
        """Marks the metadata as manually edited if the user types in the fields."""
        if not getattr(self, '_is_autofilling', False):
            self._user_touched_metadata = True

    def _browse_dest(self) -> None:
        """Prompts the user for the library root."""
        path = filedialog.askdirectory(title="Select Library Root")
        if path: self.dest_var.set(path)

    def _start_rip_thread(self) -> None:
        """Collects inputs and launches the background workers."""

        # 1. Clear artwork from previous rip (if any)
        self.lbl_art.configure(image='', text="Waiting...")
        self.lbl_art.image = None
        self.lbl_art.update_idletasks()

        # Change state to State #3 - Rip in progress
        self._save_config()
        self.is_ripping = True
        self.btn_rip.config(state="disabled", text="Ripping in Progress...")

        # Collect arguments for the rip
        source = self.src_var.get().strip()
        artist = self.artist_var.get().strip()
        album = self.album_var.get().strip()
        music_lib_root = self.dest_var.get().strip()

        # Translate the UI Profile into orthogonal backend parameters
        selected_string = self.output_format_var.get()
        profile = OutputProfile.from_display_string(selected_string)
        output_container = profile.container
        preferred_codec = profile.codec

        self.progress_bar.config(mode='indeterminate')
        self.progress_bar.start(50)  # 50 ms. refresh interval

        # Pass the new parameters to the thread
        thread = threading.Thread(target=self._run_logic,
                                  args=(source, artist, album, music_lib_root, output_container, preferred_codec))
        thread.daemon = True
        thread.start()

    def _run_logic(self, source: str, artist: str, album: str, music_lib_root: str, output_container: str,
                   preferred_codec: str) -> None:
        """The worker thread function."""
        try:
            carat.rip_album_to_library(source, artist, album, music_lib_root, output_container, preferred_codec)
            self.log_queue.put("[+] Process Complete.")
        except Exception as e:
            self.log_queue.put(f"CRITICAL ERROR: {e}")
        finally:
            # noinspection PyTypeChecker
            self.parent.after(0, self._finalize_ui)

    def _finalize_ui(self):
        self.progress_bar.stop()
        self.progress_bar.config(mode='determinate')
        self.progress_var.set(100)
        self.lbl_status.config(text="Idle")

        self.is_ripping = False
        self.btn_rip.config(state="disabled", text="Rip Complete")  # State 4: Complete


    def _display_cover(self, path: Path) -> None:
        """Updates the cover art label using Pillow."""
        self.current_cover_path = path
        try:
            pil_img = Image.open(path)
            pil_img.thumbnail((200, 200))
            img = ImageTk.PhotoImage(pil_img)
            self.lbl_art.config(image=img, text="")
            self.lbl_art.image = img
        except (OSError, tk.TclError):
            self.lbl_art.config(text="[Image Error]", image="")

    # noinspection PyUnusedLocal
    def _change_cover_art(self, event: object) -> None:
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

    def _start_queue_poller(self) -> None:
        """Consumes queue events and updates the UI (must run on the main thread)."""
        # Bulk log insertion for responsiveness
        if not self.log_queue.empty():
            self.txt_log.config(state="normal")
            logs = []
            while not self.log_queue.empty():
                try:
                    logs.append(self.log_queue.get_nowait())
                except queue.Empty:
                    break
            if logs:
                self.txt_log.insert("end", "\n".join(logs) + "\n")
                self.txt_log.see("end")
            self.txt_log.config(state="disabled")

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

            # ONLY switch to indeterminate (bounce) if we are remuxing WITHOUT a percentage
            if "Remuxing" in msg and "%" not in msg and self.progress_bar.cget("mode") != "indeterminate":
                self.progress_bar.config(mode="indeterminate")
                self.progress_bar.start(50)  # 50 ms. refresh interval
                self.btn_rip.config(text="Remuxing...")

            # If we DO have a percentage (bracket style), ensure we stay Determinate
            # --- PATCH: Also catch "Merging" for mkvmerge ---
            elif ("Remuxing" in msg or "Merging" in msg) and "[" in msg and "%]" in msg:
                if self.progress_bar.cget("mode") != "determinate":
                    self.progress_bar.stop()
                    self.progress_bar.config(mode="determinate")

                if "Merging" in msg:
                    self.btn_rip.config(text="Merging Audio Files...")
                else:
                    self.btn_rip.config(text="Remuxing...")

        while not self.art_queue.empty():
            self._display_cover(self.art_queue.get_nowait())

        # noinspection PyTypeChecker
        self.parent.after(100, self._start_queue_poller)

    def _clear_console(self) -> None:
        """Wipes the console text box clean."""
        self.txt_log.config(state="normal")
        self.txt_log.delete('1.0', tk.END)
        self.txt_log.config(state="disabled")

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

if __name__ == "__main__":
    root = tk.Tk()
    app = CaratGUI(root)
    carat.init(app.handle_fatal_error)
    root.mainloop()
