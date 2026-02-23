import itertools
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import queue
import json
import shutil
import time
from pathlib import Path
from PIL import Image, ImageTk

import carat  # The backend logic

CONFIG_FILE = Path.home() / ".carat_config.json"


class CaratGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Carat: Concise Atmos Ripping Automation Tool")
        self.root.geometry("850x850") # Square, like an album cover ;)

        try:
            img_icon = tk.PhotoImage(file='assets/carat_logo.png')
            self.root.iconphoto(False, img_icon)
        except Exception:
            pass  # Failsafe: falls back to the default feather if the image is missing

        # Load config first so we can use it in UI init
        self.config = self._load_config()

        # Initialize thread-safe queues
        self.log_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.art_queue = queue.Queue()

        self.current_cover_path = None
        self.is_ripping = False

        self._init_ui()
        self._start_queue_poller()

    @staticmethod
    def _load_config():
        """Loads user preferences from the home directory."""
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text())
            except:
                pass
        return {}

    def _save_config(self):
        """Saves current paths to the config file."""
        cfg = {
            "last_source": self.src_var.get(),
            "library_root": self.dest_var.get()
        }
        try:
            CONFIG_FILE.write_text(json.dumps(cfg))
        except:
            pass

    def _evaluate_button_state(self, *args):
        """Instantly updates the RIP button based on current inputs and state."""
        # State 3 Guard: If we are actively ripping, ignore typing
        if self.is_ripping:
            return

            # Check if all four fields have text in them
        if not all([self.src_var.get().strip(), self.dest_var.get().strip(),
                    self.artist_var.get().strip(), self.album_var.get().strip()]):
            # State 1: Not Ready
            self.btn_rip.config(state="disabled", text="FILL MISSING FIELDS")
        else:
            # State 2: Ready (This also acts as the reset for State 4 when a user edits a field)
            self.btn_rip.config(state="normal", text="RIP ATMOS")

    def _init_ui(self):
        """Constructs the visual interface."""
        style = ttk.Style()
        style.theme_use('clam')

        # Override the disabled button state for high visibility (This includes "RIP COMPLETE!")
        style.map('TButton', foreground=[('disabled', 'black')], background=[('disabled', '#e0e0e0')])

        # 1. Destination (Library Root)
        section = itertools.count(1)
        frame_dest = ttk.LabelFrame(self.root, text=f"{next(section)}. Library Root", padding=10)
        frame_dest.pack(fill="x", padx=10, pady=5)

        default_root = str(Path.cwd() / "output_library")
        self.dest_var = tk.StringVar(value=self.config.get("library_root", default_root))
        ttk.Entry(frame_dest, textvariable=self.dest_var).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(frame_dest, text="Browse...", command=self._browse_dest).pack(side="right")

        # 2. Source Selection
        frame_src = ttk.LabelFrame(self.root, text=f"{next(section)}. Source (Disc, ISO, or Folder)", padding=10)
        frame_src.pack(fill="x", padx=10, pady=5)

        self.src_var = tk.StringVar(value=self.config.get("last_source", ""))
        ttk.Entry(frame_src, textvariable=self.src_var).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(frame_src, text="Folder/Disc...", command=self._browse_source_folder).pack(side="right", padx=(2, 0))
        ttk.Button(frame_src, text="File...", command=self._browse_source_file).pack(side="right")

        # 3. Metadata & Art Container
        frame_meta_cont = ttk.Frame(self.root)
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

        frame_meta.columnconfigure(1, weight=1)

        # Cover Art Sub-Frame
        frame_art = ttk.LabelFrame(frame_meta_cont, text="Cover Art", padding=10)
        frame_art.pack(side="right", fill="y", padx=(5, 0))

        # The Anti-Jump Container: Forces exactly 200x200 pixels
        art_container = ttk.Frame(frame_art, width=200, height=200)
        art_container.pack()
        art_container.pack_propagate(False)  # Prevents container from shrinking to fit the text

        # The Label inside the container (drop the 'width=15' text sizing)
        self.lbl_art = ttk.Label(art_container, text="Waiting...", anchor="center", background="#eee")
        self.lbl_art.pack(fill="both", expand=True)
        self.lbl_art.bind("<Button-1>", self._change_cover_art)

        # 4. Action Buttons
        frame_actions = ttk.Frame(self.root)
        frame_actions.pack(fill="x", padx=15, pady=10)

        self.btn_rip = ttk.Button(frame_actions, text="RIP ATMOS", command=self._start_rip_thread)
        self.btn_rip.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.btn_clear = ttk.Button(frame_actions, text="Clear Console", command=self._clear_console)
        self.btn_clear.pack(side="right")

        # Progress & Status
        frame_prog = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        frame_prog.pack(fill="x", padx=10)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(frame_prog, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x")
        self.lbl_status = ttk.Label(frame_prog, text="Ready", font=('Segoe UI', 9, 'italic'), foreground="gray")
        self.lbl_status.pack(anchor="w", pady=(2, 0))

        # Console Output
        frame_log = ttk.LabelFrame(self.root, text="Console Output", padding=10)
        frame_log.pack(fill="both", expand=True, padx=10, pady=10)
        self.txt_log = scrolledtext.ScrolledText(frame_log, state="disabled", font=('Consolas', 9))
        self.txt_log.pack(fill="both", expand=True)

        # Bind the variables to the state evaluator
        self.src_var.trace_add("write", self._evaluate_button_state)
        self.dest_var.trace_add("write", self._evaluate_button_state)
        self.artist_var.trace_add("write", self._evaluate_button_state)
        self.album_var.trace_add("write", self._evaluate_button_state)

        # Force an initial evaluation on startup
        self._evaluate_button_state()

    def _browse_source_file(self):
        path = filedialog.askopenfilename(filetypes=[("Media File", "*.iso *.mkv *.mp4")])
        if path: self.src_var.set(path)

    def _browse_source_folder(self):
        path = filedialog.askdirectory(title="Folder of media files or Blu-ray Drive")
        if path: self.src_var.set(path)

    def _browse_dest(self):
        """Prompts the user for the library root."""
        path = filedialog.askdirectory(title="Select Library Root")
        if path: self.dest_var.set(path)

    def _log_callback(self, msg, is_progress=False):
        """Thread-safe logging back to the UI."""
        if is_progress:
            self.status_queue.put(msg)
            if "%" in msg:
                try:
                    val = float(msg.split(":")[1].strip().replace("%", ""))
                    self.progress_queue.put(val)
                except:
                    pass
        else:
            self.log_queue.put(msg)

    def _start_rip_thread(self):
        """Collects inputs and launches the background worker."""
        self._save_config()

        self.is_ripping = True
        self.btn_rip.config(state="disabled", text="RIPPING IN PROGRESS...") # State #3 Rip in progress

        source = self.src_var.get().strip()
        artist = self.artist_var.get().strip()
        album = self.album_var.get().strip()
        root = self.dest_var.get().strip()

        self._save_config()
        self.btn_rip.config(state="disabled")
        self.progress_bar.config(mode='indeterminate')
        self.progress_bar.start(10)

        # Hardcode the expected Atmos suffix for cover art polling
        expected_cover = Path(root) / artist / f"{album} (Atmos)" / "cover.jpg"

        thread = threading.Thread(target=self._run_logic, args=(source, artist, album, root))
        thread.daemon = True
        thread.start()

        threading.Thread(target=self._poll_for_cover, args=(expected_cover,), daemon=True).start()

    def _run_logic(self, source, artist, album, root):
        """The worker thread function."""
        try:
            # Assuming carat.process_release was updated to drop the suffix argument
            carat.rip_album_to_library(source, artist, album, root, self._log_callback)
            self.log_queue.put("[+] Process Complete.")
        except Exception as e:
            self.log_queue.put(f"CRITICAL ERROR: {e}")
        finally:
            self.root.after(0, self._finalize_ui)

    def _finalize_ui(self):
        self.progress_bar.stop()
        self.progress_bar.config(mode='determinate')
        self.progress_var.set(100)
        self.lbl_status.config(text="Idle")

        self.is_ripping = False
        self.btn_rip.config(state="disabled", text="RIP COMPLETE")  # State 4: Complete

    def _poll_for_cover(self, path):
        """Watches for the cover art file to appear."""
        for _ in range(10000):
            if path.exists() and path.stat().st_size > 0:
                self.art_queue.put(str(path))
                return
            time.sleep(1)

    def _display_cover(self, path):
        """Updates the cover art label using Pillow."""
        self.current_cover_path = path
        try:
            pil_img = Image.open(path)
            pil_img.thumbnail((200, 200))
            img = ImageTk.PhotoImage(pil_img)
            self.lbl_art.config(image=img, text="")
            self.lbl_art.image = img
        except Exception:
            self.lbl_art.config(text="[Image Error]", image="")

    def _change_cover_art(self, event):
        """Allows user to click and replace the cover art manually."""
        if not self.current_cover_path: return
        new_path = filedialog.askopenfilename(filetypes=[("Images", "*.jpg *.png")])
        if new_path:
            try:
                shutil.copy(new_path, self.current_cover_path)
                self._display_cover(self.current_cover_path)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to update cover: {e}")

    def _start_queue_poller(self):
        """Consumes queue events and updates the UI (must run on main thread)."""
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.txt_log.config(state="normal")
            self.txt_log.insert("end", f"{msg}\n")
            self.txt_log.see("end")
            self.txt_log.config(state="disabled")

        while not self.progress_queue.empty():
            self.progress_bar.stop()  # Kills the ghost animation!
            self.progress_bar.config(mode='determinate')
            self.progress_var.set(self.progress_queue.get_nowait())

        while not self.status_queue.empty():
            msg = self.status_queue.get_nowait()
            self.lbl_status.config(text=msg)

            # Switch to bouncing mode when FFmpeg starts
            if "Transcoding" in msg and self.progress_bar.cget("mode") != "indeterminate":
                self.progress_bar.config(mode="indeterminate")
                self.progress_bar.start(15)

        while not self.art_queue.empty():
            self._display_cover(self.art_queue.get_nowait())

        self.root.after(100, self._start_queue_poller)

    def _clear_console(self):
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

        messagebox.showerror("Carat: Startup Error", message, parent=self.root)
        self.root.destroy()
        sys.exit(1)

if __name__ == "__main__":
    root = tk.Tk()
    app = CaratGUI(root)
    carat.init_toolset(app.handle_fatal_error)
    root.mainloop()
