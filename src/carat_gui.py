import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import queue
import sys
import platform
import os
import json
import shutil
import time
from pathlib import Path
from PIL import Image, ImageTk

import carat


CONFIG_FILE = Path.home() / ".carat_config.json"

class CaratGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Carat: Concise Atmos Ripping Automation Tool")
        self.root.geometry("650x850")
        
        self.config = self._load_config()
        self.log_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.art_queue = queue.Queue()
        self.current_cover_path = None
        
        self._init_ui()
        self._start_queue_poller()
        
    def _init_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TButton", padding=6, font=('Segoe UI', 10))
        style.configure("TLabel", font=('Segoe UI', 10))
        
        # 1. Source
        frame_src = ttk.LabelFrame(self.root, text="1. Source", padding=10)
        frame_src.pack(fill="x", padx=10, pady=5)
        self.drive_var = tk.StringVar(value=self.config.get("last_drive", ""))
        self.drives = self._scan_optical_drives()
        self.drive_combo = ttk.Combobox(frame_src, textvariable=self.drive_var, values=self.drives, state="normal")
        if not self.drive_var.get() and self.drives: self.drive_combo.current(0)
        self.drive_combo.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(frame_src, text="Browse File...", command=self._browse_source).pack(side="right")

        # 2. Destination
        frame_dest = ttk.LabelFrame(self.root, text="2. Library Root", padding=10)
        frame_dest.pack(fill="x", padx=10, pady=5)
        # Default is now generic, but remembers user choice
        default_root = str(Path.cwd() / "output_library")
        self.dest_var = tk.StringVar(value=self.config.get("library_root", default_root))
        ttk.Entry(frame_dest, textvariable=self.dest_var).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(frame_dest, text="Browse...", command=self._browse_dest).pack(side="right")

        # 3. Metadata & Art
        frame_meta_cont = ttk.Frame(self.root)
        frame_meta_cont.pack(fill="x", padx=10, pady=5)
        frame_meta = ttk.LabelFrame(frame_meta_cont, text="3. Metadata", padding=10)
        frame_meta.pack(side="left", fill="both", expand=True, padx=(0, 5))
        
        ttk.Label(frame_meta, text="Artist:").grid(row=0, column=0, sticky="w")
        self.entry_artist = ttk.Entry(frame_meta)
        self.entry_artist.grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        
        # NEW: Album Line with Suffix
        ttk.Label(frame_meta, text="Album:").grid(row=1, column=0, sticky="w")
        
        frame_album_line = ttk.Frame(frame_meta)
        frame_album_line.grid(row=1, column=1, sticky="ew", padx=5, pady=5)
        
        self.entry_album = ttk.Entry(frame_album_line)
        self.entry_album.pack(side="left", fill="x", expand=True)
        
        # Suffix Field (Default: " (Atmos)")
        self.entry_suffix = ttk.Entry(frame_album_line, width=12)
        self.entry_suffix.insert(0, " (Atmos)")
        self.entry_suffix.pack(side="right", padx=(5, 0))
        
        frame_meta.columnconfigure(1, weight=1)

        frame_art = ttk.LabelFrame(frame_meta_cont, text="Cover Art", padding=10)
        frame_art.pack(side="right", fill="y", padx=(5, 0))
        self.lbl_art = ttk.Label(frame_art, text="Waiting...", anchor="center", background="#eee", width=15)
        self.lbl_art.pack(fill="both", expand=True)
        self.lbl_art.bind("<Button-1>", self._change_cover_art)
        
        ttk.Label(frame_art, text="(Click to Replace)", font=('Segoe UI', 8)).pack()

        # 4. Action
        frame_action = ttk.Frame(self.root, padding=10)
        frame_action.pack(fill="x", padx=10)
        self.led_canvas = tk.Canvas(frame_action, width=30, height=30, highlightthickness=0)
        self.led = self.led_canvas.create_oval(5, 5, 25, 25, fill="lightgray", outline="gray")
        self.led_canvas.pack(side="left", padx=(0, 10))
        self.btn_rip = ttk.Button(frame_action, text="RIP ATMOS", command=self._start_rip_thread)
        self.btn_rip.pack(side="left", fill="x", expand=True)

        # Progress
        frame_prog = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        frame_prog.pack(fill="x", padx=10)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(frame_prog, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x")
        self.lbl_status = ttk.Label(frame_prog, text="Ready", font=('Segoe UI', 9, 'italic'), foreground="gray")
        self.lbl_status.pack(anchor="w", pady=(2,0))

        # Console
        frame_log = ttk.LabelFrame(self.root, text="Console Output", padding=10)
        frame_log.pack(fill="both", expand=True, padx=10, pady=10)
        self.txt_log = scrolledtext.ScrolledText(frame_log, state="disabled", font=('Consolas', 9), height=12)
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.tag_config("err", foreground="red")
        self.txt_log.tag_config("suc", foreground="green")
        self.txt_log.tag_config("progress", foreground="blue")

    def _scan_optical_drives(self):
        drives = ["Disc 0 (Auto-Detected)"]
        if platform.system() == "Windows":
            try:
                import ctypes
                bitmask = ctypes.windll.kernel32.GetLogicalDrives()
                for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                    if bitmask & 1:
                        if ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 5:
                            drives.append(f"Drive {letter}:")
                    bitmask >>= 1
            except Exception: pass
        return drives

    def _browse_source(self):
        path = filedialog.askopenfilename(filetypes=[("Disc Image / MKV", "*.iso *.mkv")])
        if not path: path = filedialog.askdirectory(title="Select Folder")
        if path:
            self.drive_var.set(path)
            if path not in self.drives: self.drive_combo['values'] = [path] + list(self.drives)

    def _browse_dest(self):
        path = filedialog.askdirectory()
        if path: self.dest_var.set(path)

    def _load_config(self):
        if CONFIG_FILE.exists():
            try: return json.loads(CONFIG_FILE.read_text())
            except: pass
        return {}

    def _save_config(self):
        cfg = { "last_drive": self.drive_var.get(), "library_root": self.dest_var.get() }
        try: CONFIG_FILE.write_text(json.dumps(cfg))
        except: pass

    def _log(self, msg, is_progress=False):
        msg = msg.strip()
        if not msg: return
        if is_progress:
            try:
                if "Copying:" in msg:
                    val = float(msg.split(":")[1].strip().replace("%",""))
                    self.progress_queue.put(val)
                    self.status_queue.put(msg)
                elif "Transcoding:" in msg:
                    self.status_queue.put(msg)
            except: pass
            self.log_queue.put(("PROG", msg))
        else:
            self.log_queue.put(("TEXT", msg))

    def _start_queue_poller(self):
        while not self.log_queue.empty():
            type_, msg = self.log_queue.get_nowait()
            self.txt_log.config(state="normal")
            
            if type_ == "PROG":
                ranges = self.txt_log.tag_ranges("dynamic")
                if ranges:
                    self.txt_log.delete(ranges[0], ranges[1])
                self.txt_log.insert("end", msg + "\n", ("progress", "dynamic"))
            else:
                self.txt_log.tag_remove("dynamic", "1.0", "end")
                tag = "err" if "Error" in msg or "Die" in msg else "suc" if "Complete" in msg else None
                self.txt_log.insert("end", msg + "\n", tag)

            self.txt_log.see("end")
            self.txt_log.config(state="disabled")

        while not self.progress_queue.empty():
            val = self.progress_queue.get_nowait()
            self.progress_bar.stop()
            self.progress_bar.config(mode='determinate')
            self.progress_var.set(val)

        while not self.status_queue.empty():
            self.lbl_status.config(text=self.status_queue.get_nowait())

        while not self.art_queue.empty():
            path = self.art_queue.get_nowait()
            self._display_cover(path)

        self.root.after(100, self._start_queue_poller)

    def _display_cover(self, path):
        self.current_cover_path = path
        try:
            pil_img = Image.open(path)
            pil_img.thumbnail((120, 120)) 
            img = ImageTk.PhotoImage(pil_img)
            self.lbl_art.config(image=img, text="")
            self.lbl_art.image = img 
        except Exception:
            self.lbl_art.config(text="[Image Error]", image="")

    def _change_cover_art(self, event):
        if not self.current_cover_path: return
        new_path = filedialog.askopenfilename(filetypes=[("Images", "*.jpg *.png")])
        if new_path:
            try:
                shutil.copy(new_path, self.current_cover_path)
                self._display_cover(self.current_cover_path) 
                messagebox.showinfo("Updated", "Cover art updated.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed: {e}")

    def _start_rip_thread(self):
        source = self.drive_var.get()
        artist = self.entry_artist.get().strip()
        album = self.entry_album.get().strip()
        # Get Suffix (Don't strip, we want the leading space!)
        suffix = self.entry_suffix.get()
        root = self.dest_var.get().strip()
        
        if not all([artist, album, root]):
            messagebox.showwarning("Missing Info", "Please fill all fields.")
            return

        self._save_config()
        self.btn_rip.config(state="disabled")
        self.led_canvas.itemconfig(self.led, fill="yellow")
        self.lbl_art.config(image="", text="Scanning...")
        self.progress_bar.config(mode='indeterminate')
        self.progress_bar.start(10)
        self.lbl_status.config(text="Scanning source (this may take 60s)...")
        
        # Calculate expected cover path using new logic
        full_title = f"{album}{suffix}"
        expected_cover = Path(root) / artist / full_title / "cover.jpg"
        
        t = threading.Thread(target=self._run_rip, args=(source, artist, album, suffix, root, expected_cover))
        t.daemon = True
        t.start()
        self._poll_cover_thread(expected_cover)

    def _run_rip(self, source, artist, album, suffix, root, expected_cover):
        if "Auto-Detect" in source: src = "-1"
        elif "Drive" in source and ":" in source: src = "0" 
        else: src = source
        try:
            # Updated Call: Pass Suffix
            carat.process_release(src, artist, album, suffix, root, self._log)
            self.root.after(0, lambda: self._rip_finished(True))
        except Exception as e:
            self._log(f"CRITICAL ERROR: {e}")
            self.root.after(0, lambda: self._rip_finished(False))

    def _poll_cover_thread(self, path):
        def worker():
            for _ in range(300): 
                if path.exists() and path.stat().st_size > 0:
                    self.art_queue.put(str(path))
                    return
                time.sleep(1)
        t = threading.Thread(target=worker)
        t.daemon = True
        t.start()

    def _rip_finished(self, success):
        self.btn_rip.config(state="normal")
        self.progress_bar.stop()
        self.progress_bar.config(mode='determinate')
        color = "green" if success else "red"
        self.led_canvas.itemconfig(self.led, fill=color)
        if success:
            messagebox.showinfo("Done", "Library Entry Complete!")
            self.progress_var.set(100)
            self.lbl_status.config(text="Idle")
        else:
            self.lbl_status.config(text="Failed")

if __name__ == "__main__":
    root = tk.Tk()
    app = CaratGUI(root)
    root.mainloop()
