#!/usr/bin/env python3
"""NAS Backup Manager — single-tab GUI with real progress."""
import argparse
import fnmatch
import json
import os
import shutil
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "NAS Backup Manager"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

# ── Configuration ────────────────────────────────────────────────────────────


class BackupConfig:
    def __init__(self):
        self.source = ""
        self.destination = ""
        self.last_browse_dir = ""
        self.excludes = ""
        self.workers = 4

    def to_json(self):
        return {
            "source": self.source,
            "destination": self.destination,
            "last_browse_dir": self.last_browse_dir,
            "excludes": self.excludes,
            "workers": self.workers,
        }

    @classmethod
    def from_json(cls, data):
        cfg = cls()
        cfg.source = data.get("source", "")
        cfg.destination = data.get("destination", "")
        cfg.last_browse_dir = data.get("last_browse_dir", "")
        cfg.excludes = data.get("excludes", "")
        cfg.workers = int(data.get("workers", 4) or 4)
        return cfg


# ── Engine ───────────────────────────────────────────────────────────────────


class BackupEngine:
    def __init__(self, config, callbacks=None):
        self.config = config
        self.callbacks = callbacks or {}
        self.is_running = False
        self.is_paused = False
        self.stats_lock = threading.Lock()
        self.stats = {"scanned": 0, "to_copy": 0, "renamed": 0, "errors": 0, "skipped": 0, "copied": 0}
        self.copied_files = []
        self.skipped_files = []
        self.exclude_patterns = [p.strip() for p in (config.excludes or "").split(",") if p.strip()]

    def scan(self):
        """Single-pass scan: collect paths with live count, then diff."""
        self.is_running = True
        self.stats = {"scanned": 0, "to_copy": 0, "renamed": 0, "errors": 0, "skipped": 0, "copied": 0}
        self.copied_files = []
        self.skipped_files = []
        src = Path(self.config.source)
        dst = Path(self.config.destination)

        if not src.exists():
            self._log("Error", f"Source directory not found: {src}")
            return []

        find_cmd = (
            f'find "{src}" -type f \\( -iname "*.aep" -o -iname "*.nk" \\) '
            f"-print0 2>/dev/null"
        )

        # --- Collect all matching paths (shows live count) ---
        self._log("Info", "Collecting files...")
        all_files = []
        skipped = 0
        proc = os.popen(f"{find_cmd} | tr '\\0' '\\n'", "r")
        for line in proc:
            if not self.is_running:
                break
            self._wait_if_paused()
            file_path = line.strip()
            if not file_path:
                continue
            rel = os.path.relpath(file_path, src)
            if self._is_excluded(rel):
                skipped += 1
                self.skipped_files.append(rel)
                continue
            all_files.append(file_path)
            # Live count every 50 files so UI isn't spammed
            if len(all_files) % 50 == 0:
                self._update_progress("collecting", len(all_files))
        proc.close()
        self.stats["skipped"] = skipped
        if skipped:
            self._log("Info", f"Skipped {skipped} excluded file(s).")

        total = len(all_files)
        self._log("Info", f"Found {total} file(s). Checking against destination...")
        self._update_progress("scanning", 0, total)

        # --- Diff each file against destination ---
        changes = []
        for i, file_path in enumerate(all_files):
            if not self.is_running:
                break
            self._wait_if_paused()

            self.stats["scanned"] += 1
            self._update_progress("scanning", self.stats["scanned"], total)

            rel_path = os.path.relpath(file_path, src)
            dst_path = dst / rel_path

            if self._should_copy(file_path, dst_path):
                changes.append(
                    {"src": file_path, "dst": dst_path, "rel": rel_path}
                )
                self.stats["to_copy"] += 1

        self._log(
            "Info",
            f"Scan complete: {self.stats['to_copy']} to copy, "
            f"{self.stats['scanned'] - self.stats['to_copy']} up-to-date.",
        )
        return changes

    def _is_excluded(self, rel_path):
        """Match excludes against full path and each folder/file component.

        A pattern without / matches any path component, so '#Backup' excludes
        '#Backup/file.nk' and all children. Wildcards are case-insensitive.
        """
        if not self.exclude_patterns:
            return False
        norm_path = rel_path.replace(os.sep, "/").lower()
        parts = [p.lower() for p in norm_path.split("/") if p]
        for raw_pat in self.exclude_patterns:
            pat = raw_pat.replace(os.sep, "/").lower()
            if "/" in pat:
                if fnmatch.fnmatch(norm_path, pat) or fnmatch.fnmatch(f"/{norm_path}", pat):
                    return True
            else:
                if any(fnmatch.fnmatch(part, pat) for part in parts):
                    return True
                if fnmatch.fnmatch(norm_path, pat):
                    return True
        return False

    def _should_copy(self, src_path, dst_path):
        if not dst_path.exists():
            return True
        try:
            return os.stat(src_path).st_mtime > os.stat(str(dst_path)).st_mtime
        except Exception:
            return True

    def copy_files(self, changes, workers=4):
        self.is_running = True
        self.is_paused = False
        total = len(changes)
        workers = max(1, min(int(workers or 1), 16))
        self._log("Info", f"Starting copy of {total} file(s) with {workers} worker(s)...")
        self._update_progress("copying", 0, total)

        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = []
            for item in changes:
                if not self.is_running:
                    break
                self._wait_if_paused()
                futures.append(executor.submit(self._copy_one, item))

            for future in as_completed(futures):
                if not self.is_running:
                    break
                self._wait_if_paused()
                completed += 1
                error = future.result()
                if error:
                    with self.stats_lock:
                        self.stats["errors"] += 1
                    self._log("Error", error)
                else:
                    with self.stats_lock:
                        self.stats["copied"] += 1
                self._update_progress("copying", completed, total)

        self._log("Info", "Copy process finished." if self.is_running else "Copy stopped.")
        return self.stats

    def _copy_one(self, item):
        self._wait_if_paused()
        if not self.is_running:
            return None
        src_path, dst_path = item["src"], item["dst"]
        try:
            os.makedirs(dst_path.parent, exist_ok=True)
            if dst_path.exists():
                self._rename_existing(dst_path)
                with self.stats_lock:
                    self.stats["renamed"] += 1
            shutil.copy2(src_path, dst_path)
            with self.stats_lock:
                self.copied_files.append(item["rel"])
            return None
        except Exception as e:
            return f"Failed to copy {item['rel']}: {e}"

    def _rename_existing(self, dst_path):
        parent, stem, suffix = dst_path.parent, dst_path.stem, dst_path.suffix
        counter = 1
        while True:
            new_name = f"{counter}_{stem}{suffix}"
            new_path = parent / new_name
            if not new_path.exists():
                dst_path.rename(new_path)
                return counter
            counter += 1

    def pause(self):
        self.is_paused = True
        self._log("Info", "Paused.")

    def resume(self):
        self.is_paused = False
        self._log("Info", "Resumed.")

    def stop(self):
        self.is_running = False
        self.is_paused = False
        self._log("Info", "Stop requested.")

    def _wait_if_paused(self):
        while self.is_running and self.is_paused:
            time.sleep(0.1)

    def _log(self, level, msg):
        if "log" in self.callbacks:
            self.callbacks["log"](level, msg)

    def _update_progress(self, phase, value, total=None):
        if "progress" in self.callbacks:
            self.callbacks["progress"](phase, value, total)


# ── GUI ──────────────────────────────────────────────────────────────────────


class BackupApp:
    def __init__(self, root, cli_args=None):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("900x700")
        self.root.minsize(700, 500)

        self.config = BackupConfig()
        self.engine = None
        self.pending_changes = []
        self.is_scanning = False
        self.copy_start_time = None

        self._load_config()
        self._setup_ui()

        if cli_args:
            self._apply_cli_args(cli_args)

    # ── UI Layout ──────────────────────────────────────────────

    def _setup_ui(self):
        style = ttk.Style(self.root)
        style.configure("Tall.Horizontal.TProgressbar", thickness=22)
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)

        row = 0

        # -- Paths --
        path_frame = ttk.LabelFrame(main, text="Paths", padding=8)
        path_frame.grid(row=row, column=0, sticky=tk.EW, pady=(0, 8))
        path_frame.columnconfigure(1, weight=1)

        ttk.Label(path_frame, text="Source:").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        self.src_entry = ttk.Entry(path_frame)
        self.src_entry.grid(row=0, column=1, sticky=tk.EW, padx=5)
        ttk.Button(path_frame, text="Browse…", width=10, command=self._browse_src).grid(
            row=0, column=2
        )

        ttk.Label(path_frame, text="Dest:  ").grid(row=1, column=0, sticky=tk.W, padx=(0, 6), pady=(6, 0))
        self.dst_entry = ttk.Entry(path_frame)
        self.dst_entry.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=(6, 0))
        ttk.Button(path_frame, text="Browse…", width=10, command=self._browse_dst).grid(
            row=1, column=2, pady=(6, 0)
        )

        ttk.Label(path_frame, text="Exclude:").grid(row=2, column=0, sticky=tk.W, padx=(0, 6), pady=(6, 0))
        self.exclude_entry = ttk.Entry(path_frame)
        self.exclude_entry.grid(row=2, column=1, sticky=tk.EW, padx=5, pady=(6, 0))
        self.exclude_entry.insert(0, self.config.excludes)
        ttk.Label(
            path_frame, text="Patterns: *, ?  |  Comma-separated  |  Case-insensitive",
            foreground="gray", font=("", 8)
        ).grid(row=3, column=1, sticky=tk.W, padx=5)

        row += 1

        # -- Buttons --
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=row, column=0, sticky=tk.EW, pady=(0, 8))

        self.btn_scan = ttk.Button(
            btn_frame, text="🔍 Scan", command=self._start_scan, width=20
        )
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_copy = ttk.Button(
            btn_frame, text="🚀 Copy All", command=self._start_copy, width=20
        )
        self.btn_copy.pack(side=tk.LEFT, padx=(0, 10))
        self.btn_copy.configure(state="disabled")

        self.btn_pause = ttk.Button(
            btn_frame, text="⏸ Pause", command=self._toggle_pause, width=14
        )
        self.btn_pause.pack(side=tk.LEFT, padx=(0, 10))
        self.btn_pause.configure(state="disabled")

        self.btn_stop = ttk.Button(
            btn_frame, text="⏹ Stop", command=self._stop_current, width=14
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 10))
        self.btn_stop.configure(state="disabled")

        ttk.Label(btn_frame, text="Copy workers:").pack(side=tk.LEFT, padx=(10, 4))
        self.workers_var = tk.IntVar(value=self.config.workers)
        self.workers_spin = ttk.Spinbox(btn_frame, from_=1, to=16, width=4, textvariable=self.workers_var)
        self.workers_spin.pack(side=tk.LEFT)

        self.btn_import = ttk.Button(
            btn_frame, text="📂 Import Job", command=self._import_job, width=16
        )
        self.btn_import.pack(side=tk.LEFT, padx=(10, 0))

        self.btn_export = ttk.Button(
            btn_frame, text="💾 Export Job", command=self._export_job, width=16
        )
        self.btn_export.pack(side=tk.LEFT, padx=(10, 0))

        row += 1

        # -- Progress --
        prog_frame = ttk.LabelFrame(main, text="Progress", padding=8)
        prog_frame.grid(row=row, column=0, sticky=tk.EW, pady=(0, 8))
        prog_frame.columnconfigure(0, weight=1)

        self.scan_prog = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, mode="determinate", style="Tall.Horizontal.TProgressbar")
        self.scan_prog.grid(row=0, column=0, columnspan=2, sticky=tk.EW)
        self.scan_lbl = ttk.Label(prog_frame, text="Scan: idle")
        self.scan_lbl.grid(row=1, column=0, sticky=tk.W)

        self.copy_prog = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, mode="determinate", style="Tall.Horizontal.TProgressbar")
        self.copy_prog.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=(4, 0))
        self.copy_lbl = ttk.Label(prog_frame, text="Copy: idle")
        self.copy_lbl.grid(row=3, column=0, sticky=tk.W)

        row += 1

        # -- Results table --
        tree_frame = ttk.LabelFrame(main, text="Files", padding=5)
        tree_frame.grid(row=row, column=0, sticky=tk.NSEW, pady=(0, 8))
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        main.rowconfigure(row, weight=1)

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("Action", "Path"),
            show="headings",
        )
        self.tree.heading("Action", text="Action")
        self.tree.heading("Path", text="File")
        self.tree.column("Action", width=70, minwidth=60, anchor=tk.CENTER)
        self.tree.column("Path", width=700, minwidth=200)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky=tk.NSEW)
        vsb.grid(row=0, column=1, sticky=tk.NS)

        row += 1

        # -- Log --
        log_frame = ttk.LabelFrame(main, text="Log", padding=5)
        log_frame.grid(row=row, column=0, sticky=tk.EW)
        log_frame.columnconfigure(0, weight=1)

        self.log_box = tk.Text(
            log_frame, wrap=tk.WORD, height=5, state=tk.DISABLED, font=("monospace", 9)
        )
        self.log_box.grid(row=0, column=0, sticky=tk.EW)
        lsb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=lsb.set)
        lsb.grid(row=0, column=1, sticky=tk.NS)

    # ── Actions ────────────────────────────────────────────────

    def _browse_src(self):
        initial = self.config.last_browse_dir or self.src_entry.get() or "/"
        path = filedialog.askdirectory(initialdir=initial, title="Select Source Directory")
        if path:
            self.src_entry.delete(0, tk.END)
            self.src_entry.insert(0, path)
            self.config.last_browse_dir = path

    def _browse_dst(self):
        initial = self.config.last_browse_dir or self.dst_entry.get() or "/"
        path = filedialog.askdirectory(initialdir=initial, title="Select Destination Directory")
        if path:
            self.dst_entry.delete(0, tk.END)
            self.dst_entry.insert(0, path)
            self.config.last_browse_dir = path

    def _get_worker_count(self):
        try:
            value = int(str(self.workers_spin.get()).strip())
        except (TypeError, ValueError, tk.TclError):
            value = 4
        value = max(1, min(value, 16))
        self.workers_var.set(value)
        return value

    def _import_job(self):
        path = filedialog.askopenfilename(
            title="Import Backup Job",
            filetypes=[("JSON job", "*.json"), ("All files", "*")],
        )
        if not path:
            return
        try:
            with open(path, "r") as f:
                cfg = BackupConfig.from_json(json.load(f))
        except Exception as e:
            messagebox.showerror("Import failed", f"Could not import job:\n{e}")
            return

        self.config = cfg
        self.src_entry.delete(0, tk.END)
        self.src_entry.insert(0, cfg.source)
        self.dst_entry.delete(0, tk.END)
        self.dst_entry.insert(0, cfg.destination)
        self.exclude_entry.delete(0, tk.END)
        self.exclude_entry.insert(0, cfg.excludes)
        self.workers_var.set(max(1, min(int(cfg.workers or 4), 16)))
        self._log_ui("Info", f"Imported job: {path}")

    def _export_job(self):
        self.config.source = self.src_entry.get().strip()
        self.config.destination = self.dst_entry.get().strip()
        self.config.excludes = self.exclude_entry.get().strip()
        self.config.workers = self._get_worker_count()
        if not self.config.source or not self.config.destination:
            messagebox.showwarning("Missing paths", "Please set both Source and Destination before exporting.")
            return
        path = filedialog.asksaveasfilename(
            title="Export Backup Job",
            defaultextension=".json",
            filetypes=[("JSON job", "*.json"), ("All files", "*")],
        )
        if not path:
            return
        job = self.config.to_json()
        job["created_at"] = datetime.now().isoformat(timespec="seconds")
        job["app"] = APP_NAME
        with open(path, "w") as f:
            json.dump(job, f, indent=2)
        messagebox.showinfo("Exported", f"Job exported:\n{path}\n\nRun with:\npython3 backup_manager.py --job \"{path}\"")

    def _start_scan(self):
        self.config.source = self.src_entry.get().strip()
        self.config.destination = self.dst_entry.get().strip()
        self.config.excludes = self.exclude_entry.get().strip()
        self.config.workers = self._get_worker_count()

        if not self.config.source or not self.config.destination:
            messagebox.showwarning("Missing paths", "Please set both Source and Destination.")
            return

        self.btn_scan.configure(state="disabled")
        self.btn_copy.configure(state="disabled")
        self.btn_pause.configure(state="normal", text="⏸ Pause")
        self.btn_stop.configure(state="normal")
        self.tree.delete(*self.tree.get_children())
        self.is_scanning = True

        self.engine = BackupEngine(
            self.config,
            callbacks={"log": self._log_ui, "progress": self._update_ui_progress},
        )

        self.pending_changes = []
        threading.Thread(target=self._run_scan_thread, daemon=True).start()

    def _run_scan_thread(self):
        self.pending_changes = self.engine.scan()
        self.root.after(0, self._on_scan_done)

    def _on_scan_done(self):
        self.is_scanning = False
        self.btn_scan.configure(state="normal")
        self.btn_pause.configure(state="disabled", text="⏸ Pause")
        self.btn_stop.configure(state="disabled")

        for item in self.pending_changes:
            action = "Update" if Path(item["dst"]).exists() else "Copy"
            self.tree.insert("", tk.END, values=(action, item["rel"]))

        if self.pending_changes:
            self.btn_copy.configure(state="normal")

    def _start_copy(self):
        if not self.pending_changes:
            messagebox.showinfo("Info", "No files to copy. Run a scan first.")
            return

        if not messagebox.askyesno(
            "Confirm",
            f"Copy {len(self.pending_changes)} file(s)?\n"
            "Existing files will be renamed (e.g. 1_file.nk).",
        ):
            return

        self.btn_scan.configure(state="disabled")
        self.btn_copy.configure(state="disabled")
        self.btn_pause.configure(state="normal", text="⏸ Pause")
        self.btn_stop.configure(state="normal")
        self.copy_start_time = time.time()

        workers = self._get_worker_count()
        self.engine = BackupEngine(
            self.config,
            callbacks={"log": self._log_ui, "progress": self._update_ui_progress},
        )
        threading.Thread(
            target=self._copy_thread, args=(self.pending_changes, workers), daemon=True
        ).start()

    def _copy_thread(self, changes, workers):
        self.engine.copy_files(changes, workers=workers)
        self.root.after(0, self._on_copy_done)

    def _toggle_pause(self):
        if not self.engine:
            return
        if self.engine.is_paused:
            self.engine.resume()
            self.btn_pause.configure(text="⏸ Pause")
        else:
            self.engine.pause()
            self.btn_pause.configure(text="▶ Resume")

    def _stop_current(self):
        if self.engine:
            self.engine.stop()
        self.btn_stop.configure(state="disabled")
        self.btn_pause.configure(state="disabled", text="⏸ Pause")

    def _on_copy_done(self):
        self.btn_scan.configure(state="normal")
        self.btn_copy.configure(state="normal")
        self.btn_pause.configure(state="disabled", text="⏸ Pause")
        self.btn_stop.configure(state="disabled")
        if self.engine.stats["errors"] == 0:
            messagebox.showinfo("Done", "Backup completed successfully.")
        else:
            messagebox.showwarning(
                "Done with errors",
                f"Copied with {self.engine.stats['errors']} error(s).",
            )

    # ── Callbacks ──────────────────────────────────────────────

    def _log_ui(self, level, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _update_ui_progress(self, phase, value, total=None):
        if phase == "collecting":
            self.scan_prog["mode"] = "indeterminate"
            self.scan_prog.start(10)
            self.scan_lbl.configure(text=f"Collecting: {value} files found...")
        elif phase == "scanning":
            self.scan_prog.stop()
            self.scan_prog["mode"] = "determinate"
            self.scan_prog["maximum"] = max(total, 1)
            self.scan_prog["value"] = value
            self.scan_lbl.configure(text=f"Scan: {value} / {total}")
        elif phase == "copying":
            self.copy_prog["maximum"] = max(total, 1)
            self.copy_prog["value"] = value
            eta = ""
            if self.copy_start_time and value > 0 and total:
                elapsed = time.time() - self.copy_start_time
                remaining = max(total - value, 0) * (elapsed / value)
                eta = f" | ETA: {self._format_seconds(remaining)}"
            self.copy_lbl.configure(text=f"Copy: {value} / {total}{eta}")

    def _format_seconds(self, seconds):
        seconds = int(max(seconds, 0))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    # ── Config persistence ─────────────────────────────────────

    def _load_config(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    self.config = BackupConfig.from_json(json.load(f))
                self.src_entry.insert(0, self.config.source)
                self.dst_entry.insert(0, self.config.destination)
            except Exception:
                pass

    def _save_config(self):
        self.config.source = self.src_entry.get().strip()
        self.config.destination = self.dst_entry.get().strip()
        self.config.excludes = self.exclude_entry.get().strip()
        self.config.workers = self._get_worker_count()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.config.to_json(), f, indent=2)

    def _apply_cli_args(self, args):
        if args.source:
            self.src_entry.delete(0, tk.END)
            self.src_entry.insert(0, args.source)
        if args.destination:
            self.dst_entry.delete(0, tk.END)
            self.dst_entry.insert(0, args.destination)
        if args.excludes:
            self.exclude_entry.delete(0, tk.END)
            self.exclude_entry.insert(0, args.excludes)


# ── Headless CLI ─────────────────────────────────────────────────────────────


def headless_dry_run(source, destination, excludes=""):
    config = BackupConfig()
    config.source = source
    config.destination = destination
    config.excludes = excludes

    def on_log(level, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {level}: {msg}")

    def on_progress(phase, value, total=None):
        if phase == "collecting":
            print(f"  collecting {value} files...", end="\r")
            return
        bar_len = 40
        pct = value / max(total, 1)
        filled = int(bar_len * pct)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"  {phase:8} [{bar}] {value}/{total}", end="\r")

    engine = BackupEngine(config, callbacks={"log": on_log, "progress": on_progress})
    changes = engine.scan()

    print(f"\n{'='*60}")
    print(f"Scanned:  {engine.stats['scanned']}")
    print(f"To copy:  {engine.stats['to_copy']}")
    print(f"Errors:   {engine.stats['errors']}")
    print(f"Renamed:  {engine.stats['renamed']}")
    print(f"{'='*60}")
    if changes:
        print(f"\nFiles to copy ({len(changes)}):")
        for c in changes:
            print(f"  {c['rel']}")
    else:
        print("\nNo files to copy.")
    print()


def run_job(job_path, dry_run=False):
    with open(job_path, "r") as f:
        config = BackupConfig.from_json(json.load(f))

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path(job_path).with_suffix("").parent / "backup_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"backup_{run_id}.log"
    start = time.time()

    def write_log(line):
        print(line)
        with open(log_file, "a") as lf:
            lf.write(line + "\n")

    def on_log(level, msg):
        write_log(f"[{datetime.now().isoformat(timespec='seconds')}] {level}: {msg}")

    def on_progress(phase, value, total=None):
        if phase == "collecting":
            print(f"  collecting {value} files...", end="\r")
        elif total:
            print(f"  {phase}: {value}/{total}", end="\r")

    engine = BackupEngine(config, callbacks={"log": on_log, "progress": on_progress})
    write_log(f"Job: {job_path}")
    write_log(f"Source: {config.source}")
    write_log(f"Destination: {config.destination}")
    write_log(f"Excludes: {config.excludes or '(none)'}")
    changes = engine.scan()

    if not dry_run and changes:
        engine.copy_files(changes, workers=config.workers)
    elif dry_run:
        write_log("Dry run only; no files copied.")

    elapsed = time.time() - start
    write_log("--- Summary ---")
    write_log(f"Elapsed seconds: {elapsed:.1f}")
    write_log(f"Scanned: {engine.stats['scanned']}")
    write_log(f"Skipped by excludes: {engine.stats.get('skipped', 0)}")
    write_log(f"Needed copy: {engine.stats['to_copy']}")
    write_log(f"Copied: {engine.stats.get('copied', 0)}")
    write_log(f"Renamed existing: {engine.stats['renamed']}")
    write_log(f"Errors: {engine.stats['errors']}")
    write_log("--- Copied files ---")
    for rel in engine.copied_files:
        write_log(rel)
    write_log("--- Excluded/skipped files ---")
    for rel in engine.skipped_files:
        write_log(rel)
    write_log(f"Log file: {log_file}")
    return 1 if engine.stats["errors"] else 0


# ── Entry point ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--source", "-s", help="Source directory")
    parser.add_argument("--destination", "-d", help="Destination directory")
    parser.add_argument("--excludes", "-e", default="", help="Exclude patterns, comma-separated (e.g. '*backup,#backup,.tmp')")
    parser.add_argument("--dry-run", action="store_true", help="Scan and exit")
    parser.add_argument("--job", help="Run exported job JSON and copy files")
    parser.add_argument("--job-dry-run", action="store_true", help="Load exported job JSON, scan only, and write a log")
    args = parser.parse_args()

    if args.job:
        sys.exit(run_job(args.job, dry_run=args.job_dry_run))

    if args.dry_run:
        if not args.source or not args.destination:
            parser.error("--source and --destination required with --dry-run")
        headless_dry_run(args.source, args.destination, args.excludes)
        return

    root = tk.Tk()
    app = BackupApp(root, args)
    root.protocol("WM_DELETE_WINDOW", lambda: (app._save_config(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
