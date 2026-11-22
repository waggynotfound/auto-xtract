#!/usr/bin/env python3
"""
Auto Extract (GUI) - extract archives and delete each one right after it has
been extracted successfully.

Formats
  Built in : .zip, .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz,
             single-file .gz / .bz2 / .xz
  .7z/.rar : install 7-Zip (https://7-zip.org). It is found automatically.

Run:  python auto_extract_gui.py
(Rename the file to .pyw to hide the black console window on Windows.)
"""

import bz2
import gzip
import lzma
import queue
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # lets the core logic be imported on systems without Tk
    tk = None

try:
    import py7zr
except ImportError:
    py7zr = None

try:
    import rarfile
except ImportError:
    rarfile = None


# ==========================================================================
# Core extraction logic
# ==========================================================================
TAR_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".tar")
SINGLE_EXTS = (".gz", ".bz2", ".xz")
ALL_EXTS = TAR_EXTS + (".zip", ".7z", ".rar") + SINGLE_EXTS
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # hide 7-Zip console


def archive_ext(path: Path):
    name = path.name.lower()
    for ext in ALL_EXTS:
        if name.endswith(ext):
            return ext
    return None


def archive_stem(path: Path) -> str:
    ext = archive_ext(path)
    return path.name[: -len(ext)] if ext else path.stem


def unique_dir(base: Path) -> Path:
    if not base.exists():
        return base
    i = 1
    while True:
        candidate = base.with_name(f"{base.name}_{i}")
        if not candidate.exists():
            return candidate
        i += 1


def check_member_paths(dest: Path, names):
    """Block path-traversal ("zip slip") attacks."""
    root = dest.resolve()
    for name in names:
        target = (dest / name).resolve()
        if target != root and not target.is_relative_to(root):
            raise ValueError(f"Unsafe path in archive: {name}")


def extract_zip(path, dest, pwd):
    with zipfile.ZipFile(path) as zf:
        check_member_paths(dest, zf.namelist())
        zf.extractall(dest, pwd=pwd.encode() if pwd else None)


def extract_tar(path, dest, pwd):
    with tarfile.open(path) as tf:
        check_member_paths(dest, tf.getnames())
        try:
            tf.extractall(dest, filter="data")
        except TypeError:
            safe = [m for m in tf.getmembers() if not (m.issym() or m.islnk())]
            tf.extractall(dest, members=safe)


def find_7zip():
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def extract_with_7zip(exe, path, dest, pwd):
    pw = [f"-p{pwd}"] if pwd else []
    listing = subprocess.run(
        [exe, "l", "-slt", "-ba", *pw, str(path)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    if listing.returncode != 0:
        raise RuntimeError(listing.stderr.strip() or listing.stdout.strip()
                           or "7-Zip could not read the archive")
    names = [ln[7:] for ln in listing.stdout.splitlines() if ln.startswith("Path = ")]
    check_member_paths(dest, names)

    result = subprocess.run(
        [exe, "x", "-y", f"-o{dest}", *pw, str(path)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:  # only exit code 0 counts as success
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()[-300:]
                           or f"7-Zip exited with code {result.returncode}")


def extract_7z(path, dest, pwd):
    exe = find_7zip()
    if exe:
        return extract_with_7zip(exe, path, dest, pwd)
    if py7zr is None:
        raise RuntimeError("7z support needs 7-Zip installed (7-zip.org)")
    with py7zr.SevenZipFile(path, password=pwd) as sz:
        check_member_paths(dest, sz.getnames())
        sz.extractall(dest)


def extract_rar(path, dest, pwd):
    exe = find_7zip()
    if exe:
        return extract_with_7zip(exe, path, dest, pwd)
    if rarfile is None:
        raise RuntimeError("rar support needs 7-Zip installed (7-zip.org)")
    with rarfile.RarFile(path) as rf:
        check_member_paths(dest, rf.namelist())
        rf.extractall(dest, pwd=pwd)


def extract_single(path, dest, pwd):
    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}[archive_ext(path)]
    out = dest / archive_stem(path)
    with opener(path, "rb") as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)


def extract_archive(path: Path, dest: Path, pwd=None):
    ext = archive_ext(path)
    if ext in TAR_EXTS:
        extract_tar(path, dest, pwd)
    elif ext == ".zip":
        extract_zip(path, dest, pwd)
    elif ext == ".7z":
        extract_7z(path, dest, pwd)
    elif ext == ".rar":
        extract_rar(path, dest, pwd)
    elif ext in SINGLE_EXTS:
        extract_single(path, dest, pwd)
    else:
        raise ValueError("Unsupported archive type")


def find_archives(folder: Path, recursive: bool):
    pattern = "**/*" if recursive else "*"
    return sorted(p for p in folder.glob(pattern) if p.is_file() and archive_ext(p))


def run_extraction(paths, output, password, keep, recursive, nested,
                   log, progress, stop):
    """
    Extract every archive, deleting each right after it succeeds.
    log(text, tag)       - tag: info / ok / fail / warn / del
    progress(done,total) - for the progress bar
    stop                 - threading.Event; set it to stop after the current file
    Returns (extracted_count, failed_count, stopped).
    """
    work = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            work += find_archives(p, recursive)
        elif p.is_file():
            work.append(p)
        else:
            log(f"[skip] not found: {p}", "warn")

    total = len(work)
    done = failed = processed = 0
    seen = set()
    stopped = False
    progress(0, total)

    while work:
        if stop.is_set():
            stopped = True
            log("Stopped by user.", "warn")
            break

        archive = work.pop(0)
        if archive in seen or not archive.exists():
            continue
        seen.add(archive)

        if archive_ext(archive) is None:
            log(f"[skip] unsupported type: {archive}", "warn")
            processed += 1
            progress(processed, total)
            continue

        base = Path(output).expanduser() if output else archive.parent
        dest = unique_dir(base / archive_stem(archive))
        dest.mkdir(parents=True, exist_ok=True)

        try:
            log(f"Extracting  {archive}", "info")
            extract_archive(archive, dest, password or None)
        except Exception as exc:  # keep the archive when anything goes wrong
            failed += 1
            log(f"FAILED      {archive.name}: {exc}  (archive kept)", "fail")
            shutil.rmtree(dest, ignore_errors=True)
            processed += 1
            progress(processed, total)
            continue

        done += 1
        log(f"OK          -> {dest}", "ok")

        if not keep:
            try:
                archive.unlink()
                log(f"Deleted     {archive}", "del")
            except OSError as exc:
                log(f"Could not delete {archive}: {exc}", "warn")

        processed += 1
        if nested:
            new = [a for a in find_archives(dest, True) if a not in seen]
            work += new
            total += len(new)
        progress(processed, total)

    return done, failed, stopped


# ==========================================================================
# GUI
# ==========================================================================
ARCHIVE_PATTERNS = ("*.zip *.7z *.rar *.tar *.gz *.tgz *.bz2 *.tbz2 "
                    "*.xz *.txz")


class App:
    def __init__(self, root):
        self.root = root
        root.title("Auto Extract")
        root.geometry("780x680")
        root.minsize(700, 600)

        self.items = []
        self.msgs = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None

        self.var_recursive = tk.BooleanVar(value=True)
        self.var_nested = tk.BooleanVar(value=False)
        self.var_delete = tk.BooleanVar(value=True)
        self.var_output = tk.StringVar()
        self.var_password = tk.StringVar()
        self.var_status = tk.StringVar(value="Ready")

        self.build_ui()
        self.root.after(100, self.poll)

    # ---------------- layout ----------------
    def build_ui(self):
        pad = {"padx": 10, "pady": 6}
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)

        # 1. sources
        box1 = ttk.LabelFrame(main, text=" 1. Archives or folders to extract ")
        box1.grid(row=0, column=0, sticky="ew", **pad)
        box1.columnconfigure(0, weight=1)

        self.listbox = tk.Listbox(box1, height=6, selectmode="extended",
                                  activestyle="none")
        self.listbox.grid(row=0, column=0, sticky="ew", padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(box1, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=8)
        self.listbox.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(box1)
        btns.grid(row=0, column=2, padx=8, pady=8, sticky="n")
        for text, cmd in (("Add files…", self.add_files),
                          ("Add folder…", self.add_folder),
                          ("Remove selected", self.remove_selected),
                          ("Clear all", self.clear_items)):
            ttk.Button(btns, text=text, width=16, command=cmd).pack(pady=2)

        # 2. options
        box2 = ttk.LabelFrame(main, text=" 2. Options ")
        box2.grid(row=1, column=0, sticky="ew", **pad)
        box2.columnconfigure(1, weight=1)

        ttk.Checkbutton(box2, text="Include sub-folders when a folder is added",
                        variable=self.var_recursive
                        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(box2, text="Also extract archives found inside extracted archives",
                        variable=self.var_nested
                        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(box2, text="Delete each archive after it is extracted successfully "
                                   "(permanent)",
                        variable=self.var_delete
                        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=2)

        ttk.Label(box2, text="Output folder:").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(box2, textvariable=self.var_output).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Button(box2, text="Browse…", command=self.browse_output
                   ).grid(row=3, column=2, padx=8, pady=4)
        ttk.Label(box2, text="(leave empty = extract next to each archive)",
                  foreground="gray").grid(row=4, column=1, sticky="w")

        ttk.Label(box2, text="Password:").grid(row=5, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(box2, textvariable=self.var_password, show="•", width=30
                  ).grid(row=5, column=1, sticky="w", pady=4)

        # 7-Zip status
        if find_7zip():
            msg, color = "7-Zip found: .7z and .rar are supported", "#2e7d32"
        else:
            msg, color = ("7-Zip not found: .7z/.rar need it (install from 7-zip.org)",
                          "#c62828")
        ttk.Label(box2, text=msg, foreground=color
                  ).grid(row=6, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 8))

        # 3. controls
        box3 = ttk.Frame(main)
        box3.grid(row=2, column=0, sticky="ew", **pad)
        box3.columnconfigure(2, weight=1)
        self.btn_start = ttk.Button(box3, text="▶  Start", width=14, command=self.start)
        self.btn_start.grid(row=0, column=0, padx=(0, 6))
        self.btn_stop = ttk.Button(box3, text="■  Stop", width=10, command=self.stop,
                                   state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, 10))
        self.progress = ttk.Progressbar(box3, mode="determinate")
        self.progress.grid(row=0, column=2, sticky="ew")
        ttk.Label(main, textvariable=self.var_status
                  ).grid(row=3, column=0, sticky="w", padx=12)

        # 4. log
        box4 = ttk.LabelFrame(main, text=" Log ")
        box4.grid(row=4, column=0, sticky="nsew", **pad)
        box4.columnconfigure(0, weight=1)
        box4.rowconfigure(0, weight=1)
        self.log_box = tk.Text(box4, height=10, wrap="none", state="disabled",
                               font=("Consolas", 9))
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        sb2 = ttk.Scrollbar(box4, orient="vertical", command=self.log_box.yview)
        sb2.grid(row=0, column=1, sticky="ns", pady=8, padx=(0, 4))
        sb3 = ttk.Scrollbar(box4, orient="horizontal", command=self.log_box.xview)
        sb3.grid(row=1, column=0, sticky="ew", padx=(8, 0))
        self.log_box.configure(yscrollcommand=sb2.set, xscrollcommand=sb3.set)
        for tag, color in (("ok", "#2e7d32"), ("fail", "#c62828"),
                           ("warn", "#ef6c00"), ("del", "#6a1b9a")):
            self.log_box.tag_configure(tag, foreground=color)

    # ---------------- source list ----------------
    def add_item(self, path):
        path = str(Path(path))
        if path not in self.items:
            self.items.append(path)
            self.listbox.insert("end", path)

    def add_files(self):
        files = filedialog.askopenfilenames(
            title="Select archives",
            filetypes=[("Archives", ARCHIVE_PATTERNS), ("All files", "*.*")])
        for f in files:
            self.add_item(f)

    def add_folder(self):
        folder = filedialog.askdirectory(title="Select a folder containing archives")
        if folder:
            self.add_item(folder)

    def remove_selected(self):
        for index in reversed(self.listbox.curselection()):
            self.listbox.delete(index)
            del self.items[index]

    def clear_items(self):
        self.listbox.delete(0, "end")
        self.items.clear()

    def browse_output(self):
        folder = filedialog.askdirectory(title="Choose output folder")
        if folder:
            self.var_output.set(folder)

    # ---------------- logging ----------------
    def write_log(self, text, tag="info"):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ---------------- run ----------------
    def set_running(self, running):
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    def start(self):
        if not self.items:
            messagebox.showinfo("Nothing to do",
                                "Add at least one archive or folder first.")
            return
        if self.var_delete.get():
            ok = messagebox.askyesno(
                "Delete archives?",
                "Each archive will be PERMANENTLY deleted after it is extracted "
                "successfully (it does not go to the Recycle Bin).\n\nContinue?",
                icon="warning")
            if not ok:
                return

        self.stop_event.clear()
        self.set_running(True)
        self.progress.configure(value=0, maximum=1)
        self.var_status.set("Working…")
        self.write_log("---- started ----", "info")

        args = (list(self.items), self.var_output.get().strip(),
                self.var_password.get(), not self.var_delete.get(),
                self.var_recursive.get(), self.var_nested.get())
        self.worker = threading.Thread(target=self.work, args=args, daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.var_status.set("Stopping after the current file…")

    def work(self, paths, output, password, keep, recursive, nested):
        try:
            done, failed, stopped = run_extraction(
                paths, output, password, keep, recursive, nested,
                log=lambda t, tag="info": self.msgs.put(("log", t, tag)),
                progress=lambda d, t: self.msgs.put(("progress", d, t)),
                stop=self.stop_event)
            self.msgs.put(("finished", done, failed, stopped))
        except Exception as exc:
            self.msgs.put(("log", f"Unexpected error: {exc}", "fail"))
            self.msgs.put(("finished", 0, 0, False))

    def poll(self):
        try:
            while True:
                msg = self.msgs.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.write_log(msg[1], msg[2])
                elif kind == "progress":
                    self.progress.configure(maximum=max(msg[2], 1), value=msg[1])
                    self.var_status.set(f"Processed {msg[1]} of {msg[2]}")
                elif kind == "finished":
                    _, done, failed, stopped = msg
                    self.set_running(False)
                    if not stopped:
                        self.progress.configure(value=self.progress["maximum"])
                    summary = f"Finished: {done} extracted, {failed} failed."
                    if stopped:
                        summary = "Stopped. " + summary
                    self.var_status.set(summary)
                    self.write_log(summary, "fail" if failed else "ok")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    if tk is None:
        print("Tkinter is not available in this Python installation.")
        sys.exit(1)
    try:  # sharper text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    try:  # window / taskbar icon (looks next to the script, or inside the .exe)
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        root.iconbitmap(str(base / "app_icon.ico"))
    except Exception:
        pass
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()