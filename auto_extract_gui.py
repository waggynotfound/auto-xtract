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
# Theme, fonts and small custom widgets
# ==========================================================================
try:  # the extraction core stays importable on systems without Tk
    import tkinter.font as tkfont
except Exception:
    tkfont = None

COL_BG        = "#0e1014"   # window background
COL_PANEL     = "#161a21"   # cards, title bar, footer
COL_SUNKEN    = "#0b0e13"   # list and log wells
COL_FIELD     = "#1d222b"   # inputs and secondary buttons
COL_FIELD_HI  = "#252c38"   # their hover state
COL_BORDER    = "#2a3140"
COL_TEXT      = "#e6eaf2"
COL_MUTED     = "#8f99ab"
COL_FAINT     = "#6b7585"
COL_ACCENT    = "#4f8cff"
COL_ACCENT_HI = "#6ea3ff"
COL_ON_ACCENT = "#0a1120"
COL_GREEN     = "#3ddc97"
COL_RED       = "#ff6b6b"
COL_AMBER     = "#ffb454"
COL_PURPLE    = "#b48ef7"
COL_CLOSE     = "#e5484d"

F_UI = "Segoe UI"
F_MONO = "Consolas"
TAG_COLOURS = {"info": COL_TEXT, "ok": COL_GREEN, "fail": COL_RED,
               "warn": COL_AMBER, "del": COL_PURPLE, "dim": COL_MUTED}

TITLE = "Auto-Xtract"
TAGLINE = "Unpack archives and delete each one the moment it succeeds"


def init_fonts(root):
    """Use the nicest font actually installed on this machine."""
    global F_UI, F_MONO
    families = set(tkfont.families(root))
    for cand in ("Segoe UI Variable Text", "Segoe UI", "Noto Sans",
                 "DejaVu Sans", "Helvetica"):
        if cand in families:
            F_UI = cand
            break
    for cand in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New"):
        if cand in families:
            F_MONO = cand
            break


def font(size=10, weight="normal", mono=False):
    return (F_MONO if mono else F_UI, size, weight)


def asset(*parts):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def read_app_version():
    """Version from the .exe's file properties, or the VERSION file when running
    from source. Nothing in this file has to be edited to cut a release."""
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:
            import ctypes
            from ctypes import wintypes

            class FixedFileInfo(ctypes.Structure):
                _fields_ = [("dwSignature", wintypes.DWORD),
                            ("dwStrucVersion", wintypes.DWORD),
                            ("dwFileVersionMS", wintypes.DWORD),
                            ("dwFileVersionLS", wintypes.DWORD)]

            ver = ctypes.WinDLL("version")
            size = ver.GetFileVersionInfoSizeW(sys.executable, None)
            if size:
                data = ctypes.create_string_buffer(size)
                if ver.GetFileVersionInfoW(sys.executable, 0, size, data):
                    ptr = ctypes.c_void_p()
                    length = wintypes.UINT()
                    if ver.VerQueryValueW(data, "\\", ctypes.byref(ptr),
                                          ctypes.byref(length)):
                        info = ctypes.cast(ptr, ctypes.POINTER(FixedFileInfo)).contents
                        ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
                        return "%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16)
        except Exception:
            pass
    try:
        return asset("VERSION").read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle drawn as a smoothed polygon."""
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    points = (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
              x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
              x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)
    return canvas.create_polygon(points, smooth=True, **kw)


def work_area():
    """The usable desktop rectangle, minus the taskbar."""
    try:
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080


def apply_theme(root):
    """ttk is only used for the slim scrollbars; make them dark and arrow-free."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    for orient, tray in (("Vertical", "ns"), ("Horizontal", "ew")):
        name = f"Dark.{orient}.TScrollbar"
        style.layout(name, [
            (f"{orient}.Scrollbar.trough",
             {"sticky": tray, "children": [
                 (f"{orient}.Scrollbar.thumb",
                  {"expand": "1", "sticky": "nswe"})]})])
        style.configure(name, background="#333c4a", troughcolor=COL_SUNKEN,
                        bordercolor=COL_SUNKEN, darkcolor="#333c4a",
                        lightcolor="#333c4a", relief="flat",
                        borderwidth=0, width=9, gripcount=0)
        style.map(name, background=[("active", "#465061"), ("pressed", COL_ACCENT)])


def attach_scroll(bar, widget, root):
    """Keep a slim scrollbar only while the widget actually overflows."""
    def sync(first, last):
        bar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            bar.grid_remove()
        else:
            bar.grid()

    widget.configure(yscrollcommand=sync)
    root.after(200, lambda: sync(*widget.yview()))


class Card(tk.Frame):
    """A flat panel with a one-pixel border and an optional header row."""

    def __init__(self, parent, title=None, pad=14, bg=COL_PANEL):
        super().__init__(parent, bg=bg, bd=0, highlightthickness=1,
                         highlightbackground=COL_BORDER, highlightcolor=COL_BORDER)
        self.pad = pad
        self.header = None
        if title is not None:
            self.header = tk.Frame(self, bg=bg)
            self.header.pack(fill="x", padx=pad, pady=(pad - 3, 0))
            tk.Label(self.header, text=title, bg=bg, fg=COL_MUTED,
                     font=font(9, "bold")).pack(side="left")
        self.body = tk.Frame(self, bg=bg)
        self.body.pack(fill="both", expand=True, padx=pad, pady=(9, pad))


class Button(tk.Canvas):
    """Flat rounded button with hover, press and disabled states."""

    KINDS = {
        # kind      fill        text          hover         border
        "primary": (COL_ACCENT, COL_ON_ACCENT, COL_ACCENT_HI, COL_ACCENT),
        "ghost":   (COL_FIELD,  COL_TEXT,      COL_FIELD_HI,  COL_BORDER),
        "quiet":   (COL_PANEL,  COL_MUTED,     COL_FIELD,     COL_PANEL),
        "danger":  ("#3b2027",  "#ff9c9c",     "#4c2a33",     "#5a3038"),
    }

    def __init__(self, parent, text, command=None, kind="ghost",
                 bg=COL_PANEL, width=None, height=34, font_spec=None, radius=8):
        self._font_spec = font_spec or font(10)
        probe = tkfont.Font(family=self._font_spec[0], size=self._font_spec[1],
                            weight=self._font_spec[2]
                            if len(self._font_spec) > 2 else "normal")
        w = width or probe.measure(text) + 30
        super().__init__(parent, width=w, height=height, bg=bg, bd=0,
                         highlightthickness=0, cursor="hand2", takefocus=1)
        self._cw, self._ch, self._radius = w, height, radius
        self._text, self._command, self._kind = text, command, kind
        self._enabled, self._hover, self._pressed = True, False, False
        self._paint()
        self.bind("<Configure>", self._on_configure)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_configure(self, event):
        self._cw, self._ch = event.width, event.height
        self._paint()

    def _on_enter(self, _event=None):
        if self._enabled:
            self._hover = True
            self._paint()

    def _on_leave(self, _event=None):
        self._hover = self._pressed = False
        self._paint()

    def _on_press(self, _event=None):
        if self._enabled:
            self._pressed = True
            self._paint()

    def _on_release(self, event):
        was = self._pressed
        self._pressed = False
        self._paint()
        inside = 0 <= event.x <= self._cw and 0 <= event.y <= self._ch
        if was and self._enabled and inside and self._command:
            self._command()

    def set_enabled(self, on):
        self._enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self._paint()

    def set_text(self, text):
        self._text = text
        self._paint()

    def set_kind(self, kind):
        self._kind = kind
        self._paint()

    def _paint(self):
        self.delete("all")
        fill, fg, hover, border = self.KINDS.get(self._kind, self.KINDS["ghost"])
        if not self._enabled:
            fill, fg, border = COL_PANEL, COL_FAINT, COL_BORDER
        elif self._hover or self._pressed:
            fill = hover
        w, h = self._cw, self._ch
        round_rect(self, 1, 1, w - 1, h - 1, self._radius,
                   fill=fill, outline=border)
        self.create_text(w / 2, h / 2 + 1, text=self._text, fill=fg,
                         font=self._font_spec, anchor="center")


class Switch(tk.Canvas):
    """An on/off switch bound to a BooleanVar."""

    def __init__(self, parent, variable, bg=COL_PANEL, command=None):
        super().__init__(parent, width=46, height=24, bg=bg, bd=0,
                         highlightthickness=0, cursor="hand2")
        self.var, self.command = variable, command
        self.bind("<Button-1>", self._click)
        self.var.trace_add("write", lambda *_a: self._paint())
        self._paint()

    def _click(self, _event=None):
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _paint(self):
        self.delete("all")
        on = bool(self.var.get())
        round_rect(self, 1, 2, 45, 22, 10,
                   fill=COL_ACCENT if on else "#3a4250",
                   outline=COL_ACCENT if on else "#3a4250")
        cx = 34 if on else 12
        self.create_oval(cx - 9, 3, cx + 9, 21,
                         fill="#ffffff" if on else "#c2cbd9", outline="")


class ProgressBar(tk.Canvas):
    """A slim rounded progress bar."""

    def __init__(self, parent, bg=COL_PANEL, height=8):
        super().__init__(parent, height=height, bg=bg, bd=0, highlightthickness=0)
        self._value, self._max = 0.0, 1.0
        self.bind("<Configure>", lambda _e: self._paint())

    def set(self, value, maximum=None):
        self._value = float(value)
        if maximum is not None:
            self._max = float(max(maximum, 1))
        self._paint()

    def finish(self):
        self.set(self._max, self._max)

    def _paint(self):
        self.delete("all")
        w = max(self.winfo_width(), 12)
        h = max(self.winfo_height(), 6)
        round_rect(self, 0, 0, w, h, h / 2, fill="#20262f", outline="")
        if self._max > 0 and self._value > 0:
            frac = min(1.0, self._value / self._max)
            round_rect(self, 0, 0, max(w * frac, h), h, h / 2,
                       fill=COL_ACCENT, outline="")


def make_entry(parent, textvariable, show=None, width=None):
    entry = tk.Entry(parent, textvariable=textvariable, bg=COL_FIELD, fg=COL_TEXT,
                     insertbackground=COL_ACCENT, relief="flat", bd=0,
                     highlightthickness=1, highlightbackground=COL_BORDER,
                     highlightcolor=COL_ACCENT, font=font(10),
                     disabledbackground=COL_FIELD, disabledforeground=COL_FAINT,
                     selectbackground=COL_ACCENT, selectforeground=COL_ON_ACCENT)
    if show:
        entry.configure(show=show)
    if width:
        entry.configure(width=width)
    return entry


class CaptionButton(tk.Canvas):
    """Minimise / maximise / close, drawn to match the dark chrome."""

    def __init__(self, parent, kind, command, bg=COL_PANEL):
        super().__init__(parent, width=40, height=28, bg=bg, bd=0,
                         highlightthickness=0, cursor="hand2")
        self.kind, self.command, self._bg = kind, command, bg
        self._hover = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonRelease-1>", lambda _e: self.command())
        self._paint()

    def _on_enter(self, _event=None):
        self._hover = True
        self._paint()

    def _on_leave(self, _event=None):
        self._hover = False
        self._paint()

    def _paint(self):
        self.delete("all")
        closing = self.kind == "close" and self._hover
        self.configure(bg=COL_CLOSE if closing else
                       ("#242b36" if self._hover else self._bg))
        fg = "#ffffff" if closing else COL_TEXT
        cx, cy = 20, 14
        if self.kind == "min":
            self.create_line(cx - 5, cy + 1, cx + 5, cy + 1, fill=fg, width=1)
        elif self.kind == "max":
            self.create_rectangle(cx - 5, cy - 4, cx + 5, cy + 4,
                                  outline=fg, width=1)
        elif self.kind == "restore":
            self.create_rectangle(cx - 5, cy - 5, cx + 2, cy + 2,
                                  outline=fg, width=1)
            self.create_rectangle(cx - 2, cy - 2, cx + 5, cy + 5,
                                  outline=fg, width=1)
        else:
            self.create_line(cx - 5, cy - 4, cx + 5, cy + 4, fill=fg, width=1)
            self.create_line(cx - 5, cy + 4, cx + 5, cy - 4, fill=fg, width=1)


class Chrome:
    """Frameless window chrome: drag, minimise, maximise, edge resizing.

    Windows cannot recolour a Tk caption bar on every machine, so the title bar
    is drawn by the app instead: it always matches the dark theme.
    """

    EDGE = 6
    CURSORS = {"n": "size_ns", "s": "size_ns", "w": "size_we", "e": "size_we",
               "nw": "size_nw_se", "se": "size_nw_se",
               "ne": "size_ne_sw", "sw": "size_ne_sw"}

    def __init__(self, root, size=(1080, 790), min_size=(940, 660)):
        self.root = root
        self.min_w, self.min_h = min_size
        self.maximised = False
        self._restore_geom = None
        self._drag = None
        self._resize = None
        self.on_max_change = None

        root.overrideredirect(True)
        root.configure(bg=COL_BORDER)
        root.update_idletasks()
        self.hwnd = self._find_hwnd()
        self._show_in_taskbar()

        left, top, right, bottom = work_area()
        w = min(size[0], max(min_size[0], right - left - 60))
        h = min(size[1], max(min_size[1], bottom - top - 60))
        x = left + (right - left - w) // 2
        y = top + (bottom - top - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")

        root.bind("<Motion>", self._on_motion, add="+")
        root.bind("<ButtonPress-1>", self._on_press, add="+")
        root.bind("<B1-Motion>", self._on_motion_resize, add="+")
        root.bind("<ButtonRelease-1>", self._on_release, add="+")
        root.bind("<Map>", self._on_map, add="+")

    # -- window plumbing ------------------------------------------------
    def _find_hwnd(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            return hwnd or self.root.winfo_id()
        except Exception:
            return 0

    def _show_in_taskbar(self):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            GWL_EXSTYLE, WS_EX_APPWINDOW, WS_EX_TOOLWINDOW = -20, 0x40000, 0x80
            style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _on_map(self, _event=None):
        if not self.root.overrideredirect():  # re-assert after a restore
            self.root.overrideredirect(True)

    def minimise(self):
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(self.hwnd, 6)  # SW_MINIMIZE
        except Exception:
            self.root.iconify()

    def toggle_max(self):
        left, top, right, bottom = work_area()
        if self.maximised:
            if self._restore_geom:
                self.root.geometry(self._restore_geom)
            self.maximised = False
        else:
            self._restore_geom = self.root.geometry()
            self.root.geometry(f"{right - left}x{bottom - top}+{left}+{top}")
            self.maximised = True
        if self.on_max_change:
            self.on_max_change()

    def close(self):
        self.root.destroy()

    # -- dragging -------------------------------------------------------
    def attach_drag(self, *widgets):
        for widget in widgets:
            widget.bind("<ButtonPress-1>", self._drag_start, add="+")
            widget.bind("<B1-Motion>", self._drag_move, add="+")
            widget.bind("<Double-Button-1>",
                        lambda _e: self.toggle_max(), add="+")

    def _drag_start(self, event):
        if self.maximised:
            return
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        if not self._drag:
            return
        dx, dy = self._drag
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    # -- resizing -------------------------------------------------------
    def _zone(self, x, y):
        w, h = self.root.winfo_width(), self.root.winfo_height()
        left, right = x <= self.EDGE, x >= w - self.EDGE
        top, bottom = y <= self.EDGE, y >= h - self.EDGE
        if top and left:
            return "nw"
        if top and right:
            return "ne"
        if bottom and left:
            return "sw"
        if bottom and right:
            return "se"
        if top:
            return "n"
        if bottom:
            return "s"
        if left:
            return "w"
        if right:
            return "e"
        return ""

    def _on_motion(self, event):
        if self._resize or self.maximised:
            return
        zone = self._zone(event.x, event.y)
        try:
            self.root.configure(cursor=self.CURSORS.get(zone, "arrow"))
        except tk.TclError:
            pass

    def _on_press(self, event):
        if self.maximised:
            return
        zone = self._zone(event.x, event.y)
        if zone:
            self._resize = (zone, event.x_root, event.y_root,
                            self.root.winfo_width(), self.root.winfo_height(),
                            self.root.winfo_x(), self.root.winfo_y())

    def _on_motion_resize(self, event):
        if not self._resize:
            return
        zone, sx, sy, w0, h0, x0, y0 = self._resize
        dx, dy = event.x_root - sx, event.y_root - sy
        x, y, w, h = x0, y0, w0, h0
        if "e" in zone:
            w = max(self.min_w, w0 + dx)
        if "s" in zone:
            h = max(self.min_h, h0 + dy)
        if "w" in zone:
            w = max(self.min_w, w0 - dx)
            x = x0 + (w0 - w)
        if "n" in zone:
            h = max(self.min_h, h0 - dy)
            y = y0 + (h0 - h)
        self.root.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")

    def _on_release(self, _event=None):
        self._resize = None


class Dialog(tk.Toplevel):
    """A dark modal that replaces the grey system message boxes."""

    def __init__(self, parent, title, message, confirm_text="Continue",
                 cancel_text="Cancel", danger=False):
        super().__init__(parent)
        self.result = False
        self.withdraw()
        self.title(title)
        self.overrideredirect(True)
        self.configure(bg=COL_BORDER)
        self.transient(parent.winfo_toplevel())

        outer = tk.Frame(self, bg=COL_PANEL, padx=22, pady=18)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(outer, bg=COL_PANEL)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=COL_PANEL, fg=COL_TEXT,
                 font=font(11, "bold"), anchor="w").pack(side="left")
        CaptionButton(head, "close", self._cancel).pack(side="right")
        tk.Label(outer, text=message, bg=COL_PANEL, fg=COL_MUTED, font=font(10),
                 justify="left", anchor="w", wraplength=370
                 ).pack(fill="x", pady=(6, 18))

        row = tk.Frame(outer, bg=COL_PANEL)
        row.pack(fill="x")
        if cancel_text:
            Button(row, cancel_text, self._cancel, kind="ghost", bg=COL_PANEL,
                   height=34).pack(side="right")
        Button(row, confirm_text, self._ok,
               kind="danger" if danger else "primary", bg=COL_PANEL,
               height=34).pack(side="right", padx=(0, 8) if cancel_text else 0)

        for widget in (head, outer):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._ok())
        self._drag = None
        self._centre(parent)
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(60, lambda: self.attributes("-topmost", False))
        self.grab_set()
        self.focus_force()
        self.wait_window(self)

    def _drag_start(self, event):
        self._drag = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event):
        if self._drag:
            dx, dy = self._drag
            self.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _centre(self, parent):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - w) // 2
        y = top.winfo_rooty() + (top.winfo_height() - h) // 3
        self.geometry(f"+{max(x, 20)}+{max(y, 20)}")

    def _ok(self):
        self.result = True
        self.destroy()

    def _cancel(self):
        self.result = False
        self.destroy()

    @classmethod
    def ask(cls, parent, title, message, confirm_text="Continue",
            cancel_text="Cancel", danger=False):
        return cls(parent.winfo_toplevel(), title, message, confirm_text,
                   cancel_text, danger).result


# ==========================================================================
# GUI
# ==========================================================================
ARCHIVE_PATTERNS = ("*.zip *.7z *.rar *.tar *.gz *.tgz *.bz2 *.tbz2 "
                    "*.xz *.txz")


class App:
    def __init__(self, root, chrome):
        self.root = root
        self.chrome = chrome
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
        # one pixel of COL_BORDER around everything = a crisp border on a
        # frameless window
        self.shell = tk.Frame(self.root, bg=COL_BG)
        self.shell.pack(fill="both", expand=True, padx=1, pady=1)
        self.shell.columnconfigure(0, weight=1)
        self.shell.rowconfigure(2, weight=1)

        self.build_titlebar()

        tk.Frame(self.shell, bg=COL_BORDER, height=1).grid(
            row=1, column=0, sticky="ew")

        body = tk.Frame(self.shell, bg=COL_BG)
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(14, 12))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=344, weight=0)
        body.rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=COL_BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        right = tk.Frame(body, bg=COL_BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)

        self.build_sources(left)
        self.build_log(left)
        self.build_options(right)
        self.build_notes(right)
        self.build_footer()

    def build_titlebar(self):
        bar = tk.Frame(self.shell, bg=COL_PANEL)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)

        try:
            self._icon = tk.PhotoImage(file=str(asset("assets", "icon-48.png")))
            icon = tk.Label(bar, image=self._icon, bg=COL_PANEL)
        except Exception:
            self._icon = None
            icon = tk.Label(bar, text="\u25a3", bg=COL_PANEL, fg=COL_ACCENT,
                            font=font(16))
        icon.grid(row=0, column=0, rowspan=2, padx=(16, 10), pady=10, sticky="w")

        names = tk.Frame(bar, bg=COL_PANEL)
        names.grid(row=0, column=1, rowspan=2, sticky="w")
        line = tk.Frame(names, bg=COL_PANEL)
        line.pack(anchor="w")
        title = tk.Label(line, text=TITLE, bg=COL_PANEL, fg=COL_TEXT,
                         font=font(13, "bold"))
        title.pack(side="left")
        version = read_app_version()
        if version:
            tk.Label(line, text=f"v{version}", bg=COL_FIELD, fg=COL_MUTED,
                     font=font(8), padx=7, pady=1).pack(side="left", padx=(9, 0))
        tagline = tk.Label(names, text=TAGLINE, bg=COL_PANEL, fg=COL_FAINT,
                           font=font(9))
        tagline.pack(anchor="w", pady=(2, 0))

        buttons = tk.Frame(bar, bg=COL_PANEL)
        buttons.grid(row=0, column=2, sticky="ne", pady=(6, 0), padx=(0, 6))
        CaptionButton(buttons, "min", self.chrome.minimise).pack(side="left")
        self.btn_max = CaptionButton(buttons, "max", self.chrome.toggle_max)
        self.btn_max.pack(side="left")
        CaptionButton(buttons, "close", self.chrome.close).pack(side="left")
        self.chrome.on_max_change = lambda: self.btn_max.set_kind(
            "restore" if self.chrome.maximised else "max")

        self.chrome.attach_drag(bar, icon, names, line, title, tagline)

    def build_sources(self, parent):
        card = Card(parent, "  Archives and folders")
        card.grid(row=0, column=0, sticky="nsew", pady=(0, 12))
        self.count_label = tk.Label(card.header, text="0 items", bg=COL_PANEL,
                                    fg=COL_FAINT, font=font(9))
        self.count_label.pack(side="right")

        body = card.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        well = tk.Frame(body, bg=COL_SUNKEN, highlightthickness=1,
                        highlightbackground=COL_BORDER, highlightcolor=COL_BORDER)
        well.grid(row=0, column=0, sticky="nsew")
        well.columnconfigure(0, weight=1)
        well.rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(well, height=5, selectmode="extended",
                                  activestyle="none", relief="flat", bd=0,
                                  highlightthickness=0, bg=COL_SUNKEN,
                                  fg=COL_TEXT, font=font(10),
                                  selectbackground=COL_ACCENT,
                                  selectforeground=COL_ON_ACCENT,
                                  exportselection=False)
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        sb = ttk.Scrollbar(well, orient="vertical", style="Dark.Vertical.TScrollbar",
                           command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=4, padx=(0, 2))
        attach_scroll(sb, self.listbox, self.root)
        self.listbox.bind("<Delete>", lambda _e: self.remove_selected())

        self.empty_hint = tk.Label(
            well, bg=COL_SUNKEN, fg=COL_FAINT, font=font(9), justify="center",
            text="Nothing queued yet\nUse  Add files  or  Add folder  below")
        self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")

        row = tk.Frame(body, bg=COL_PANEL)
        row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        Button(row, "+  Add files", self.add_files, bg=COL_PANEL).pack(side="left")
        Button(row, "+  Add folder", self.add_folder, bg=COL_PANEL
               ).pack(side="left", padx=(8, 0))
        Button(row, "Clear all", self.clear_items, kind="quiet",
               bg=COL_PANEL).pack(side="right")
        Button(row, "Remove", self.remove_selected, kind="quiet",
               bg=COL_PANEL).pack(side="right", padx=(0, 8))

    def build_log(self, parent):
        card = Card(parent, "  Activity")
        card.grid(row=1, column=0, sticky="nsew")
        Button(card.header, "Clear log", self.clear_log, kind="quiet",
               bg=COL_PANEL, height=24, radius=6).pack(side="right")

        body = card.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        well = tk.Frame(body, bg=COL_SUNKEN, highlightthickness=1,
                        highlightbackground=COL_BORDER, highlightcolor=COL_BORDER)
        well.grid(row=0, column=0, sticky="nsew")
        well.columnconfigure(0, weight=1)
        well.rowconfigure(0, weight=1)

        self.log_box = tk.Text(well, height=8, wrap="word", bd=0, relief="flat",
                               highlightthickness=0, bg=COL_SUNKEN, fg=COL_TEXT,
                               insertbackground=COL_ACCENT, font=font(9, mono=True),
                               padx=10, pady=8, state="disabled",
                               selectbackground=COL_ACCENT,
                               selectforeground=COL_ON_ACCENT)
        self.log_box.grid(row=0, column=0, sticky="nsew")
        sb2 = ttk.Scrollbar(well, orient="vertical", style="Dark.Vertical.TScrollbar",
                            command=self.log_box.yview)
        sb2.grid(row=0, column=1, sticky="ns", pady=4, padx=(0, 2))
        attach_scroll(sb2, self.log_box, self.root)
        for tag, colour in TAG_COLOURS.items():
            self.log_box.tag_configure(tag, foreground=colour)
        self.log_hint = tk.Label(well, bg=COL_SUNKEN, fg=COL_FAINT, font=font(9),
                                 text="Results appear here as each archive is processed")
        self.log_hint.place(relx=0.5, rely=0.5, anchor="center")

    def build_options(self, parent):
        card = Card(parent, "  Options")
        card.grid(row=0, column=0, sticky="ew")
        body = card.body

        self._switch_row(body, "Include sub-folders",
                         "Scan added folders all the way down", self.var_recursive)
        self._switch_row(body, "Extract nested archives",
                         "Unpack archives found inside an extracted one",
                         self.var_nested)
        self._switch_row(body, "Delete each archive after success",
                         "Permanent: it does not go to the Recycle Bin",
                         self.var_delete, hint_colour=COL_AMBER)

        tk.Frame(body, bg=COL_BORDER, height=1).pack(fill="x", pady=10)

        tk.Label(body, text="Output folder", bg=COL_PANEL, fg=COL_TEXT,
                 font=font(10)).pack(anchor="w")
        out_row = tk.Frame(body, bg=COL_PANEL)
        out_row.pack(fill="x", pady=(5, 2))
        out_row.columnconfigure(0, weight=1)
        make_entry(out_row, self.var_output).grid(row=0, column=0, sticky="ew", ipady=5)
        Button(out_row, "Browse", self.browse_output, bg=COL_PANEL, height=30
               ).grid(row=0, column=1, padx=(8, 0))
        tk.Label(body, text="Leave empty to extract next to each archive",
                 bg=COL_PANEL, fg=COL_FAINT, font=font(8)).pack(anchor="w")

        tk.Label(body, text="Password", bg=COL_PANEL, fg=COL_TEXT,
                 font=font(10)).pack(anchor="w", pady=(10, 0))
        pw_row = tk.Frame(body, bg=COL_PANEL)
        pw_row.pack(fill="x", pady=(5, 0))
        pw_row.columnconfigure(0, weight=1)
        self.pw_entry = make_entry(pw_row, self.var_password, show="\u2022")
        self.pw_entry.grid(row=0, column=0, sticky="ew", ipady=5)
        self.btn_eye = Button(pw_row, "Show", self.toggle_password, kind="quiet",
                              bg=COL_PANEL, height=30, width=54)
        self.btn_eye.grid(row=0, column=1, padx=(8, 0))

        tk.Frame(body, bg=COL_BORDER, height=1).pack(fill="x", pady=10)
        self.build_zip_status(body)

    def build_zip_status(self, parent):
        row = tk.Frame(parent, bg=COL_PANEL)
        row.pack(fill="x")
        exe = find_7zip()
        colour = COL_GREEN if exe else COL_AMBER
        dot = tk.Canvas(row, width=10, height=10, bg=COL_PANEL, bd=0,
                        highlightthickness=0)
        dot.create_oval(1, 1, 9, 9, fill=colour, outline="")
        dot.pack(side="left", pady=(3, 0))
        text = ("7-Zip found: .7z and .rar work" if exe
                else "No 7-Zip: .7z and .rar unavailable")
        tk.Label(row, text=text, bg=COL_PANEL, fg=COL_MUTED, font=font(9)
                 ).pack(side="left", padx=(7, 0))
        if not exe:
            Button(parent, "Get 7-Zip", self.open_7zip, kind="quiet",
                   bg=COL_PANEL, height=26, radius=6
                   ).pack(anchor="w", pady=(6, 0))

    def build_notes(self, parent):
        card = Card(parent, "  Good to know")
        card.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        body = card.body
        for head, text in (
            ("Delete only on success",
             "A failed or corrupt extraction keeps the archive."),
            ("No half-extracted debris",
             "A failed output folder is removed again."),
            ("Zip Slip blocked",
             "Member paths are checked before anything is written."),
        ):
            block = tk.Frame(body, bg=COL_PANEL)
            block.pack(fill="x", pady=(0, 8))
            tk.Label(block, text=head, bg=COL_PANEL, fg=COL_TEXT,
                     font=font(9, "bold"), anchor="w").pack(fill="x")
            tk.Label(block, text=text, bg=COL_PANEL, fg=COL_FAINT, font=font(8),
                     anchor="w", justify="left", wraplength=285).pack(fill="x")

    def build_footer(self):
        foot = tk.Frame(self.shell, bg=COL_BORDER)
        foot.grid(row=3, column=0, sticky="ew", pady=(1, 0))
        bar = tk.Frame(foot, bg=COL_PANEL)
        bar.pack(fill="x", padx=1)
        bar.columnconfigure(3, weight=1)

        self.btn_start = Button(bar, "\u25b6   Start", self.start, kind="primary",
                                bg=COL_PANEL, height=38, width=128)
        self.btn_start.grid(row=0, column=0, padx=(16, 8), pady=12)
        self.btn_stop = Button(bar, "\u25a0   Stop", self.stop, bg=COL_PANEL,
                               height=38, width=100)
        self.btn_stop.grid(row=0, column=1, pady=12)
        self.btn_stop.set_enabled(False)
        self.progress = ProgressBar(bar, bg=COL_PANEL, height=8)
        self.progress.grid(row=0, column=3, sticky="ew", padx=(18, 16))
        tk.Label(bar, textvariable=self.var_status, bg=COL_PANEL, fg=COL_MUTED,
                 font=font(9), anchor="e", width=26).grid(row=0, column=4,
                                                         padx=(0, 16))

    # ---------------- helpers ----------------
    def _switch_row(self, parent, title, hint, var, hint_colour=COL_FAINT):
        row = tk.Frame(parent, bg=COL_PANEL)
        row.pack(fill="x", pady=(0, 8))
        row.columnconfigure(0, weight=1)
        tk.Label(row, text=title, bg=COL_PANEL, fg=COL_TEXT, font=font(10),
                 anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(row, text=hint, bg=COL_PANEL, fg=hint_colour, font=font(8),
                 anchor="w").grid(row=1, column=0, sticky="w", pady=(1, 0))
        Switch(row, var, bg=COL_PANEL).grid(row=0, column=1, rowspan=2,
                                            sticky="e", padx=(12, 0))

    def _update_count(self):
        n = len(self.items)
        self.count_label.configure(text=f"{n} item" if n == 1 else f"{n} items")
        if n:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")

    # ---------------- source list ----------------
    def add_item(self, path):
        path = str(Path(path))
        if path not in self.items:
            self.items.append(path)
            self.listbox.insert("end", path)
            self._update_count()

    def add_files(self):
        files = filedialog.askopenfilenames(
            parent=self.root, title="Select archives",
            filetypes=[("Archives", ARCHIVE_PATTERNS), ("All files", "*.*")])
        for f in files:
            self.add_item(f)

    def add_folder(self):
        folder = filedialog.askdirectory(
            parent=self.root, title="Select a folder containing archives")
        if folder:
            self.add_item(folder)

    def remove_selected(self):
        for index in reversed(self.listbox.curselection()):
            self.listbox.delete(index)
            del self.items[index]
        self._update_count()

    def clear_items(self):
        self.listbox.delete(0, "end")
        self.items.clear()
        self._update_count()

    def browse_output(self):
        folder = filedialog.askdirectory(parent=self.root, title="Choose output folder")
        if folder:
            self.var_output.set(folder)

    def toggle_password(self):
        hidden = self.pw_entry.cget("show") != ""
        self.pw_entry.configure(show="" if hidden else "\u2022")
        self.btn_eye.set_text("Hide" if hidden else "Show")

    def open_7zip(self):
        import webbrowser
        webbrowser.open("https://7-zip.org/")

    # ---------------- logging ----------------
    def write_log(self, text, tag="info"):
        self.log_hint.place_forget()
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.log_hint.place(relx=0.5, rely=0.5, anchor="center")

    # ---------------- run ----------------
    def set_running(self, running):
        self.btn_start.set_enabled(not running)
        self.btn_stop.set_enabled(running)

    def start(self):
        if not self.items:
            Dialog.ask(self.root, "Nothing to do",
                       "Add at least one archive or folder first, then press Start.",
                       confirm_text="OK", cancel_text=None)
            return
        if self.var_delete.get():
            ok = Dialog.ask(
                self.root, "Delete the archives?",
                "Each archive is permanently deleted once it has been extracted "
                "successfully.\n\nIt does not go to the Recycle Bin, and it cannot "
                "be undone.", confirm_text="Delete and extract", danger=True)
            if not ok:
                return

        self.stop_event.clear()
        self.set_running(True)
        self.progress.set(0, 1)
        self.var_status.set("Working\u2026")
        self.write_log("\u2500\u2500 started \u2500\u2500", "dim")

        args = (list(self.items), self.var_output.get().strip(),
                self.var_password.get(), not self.var_delete.get(),
                self.var_recursive.get(), self.var_nested.get())
        self.worker = threading.Thread(target=self.work, args=args, daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.var_status.set("Stopping after this file\u2026")

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
                    self.progress.set(msg[1], max(msg[2], 1))
                    self.var_status.set(f"{msg[1]} / {msg[2]}")
                elif kind == "finished":
                    _, done, failed, stopped = msg
                    self.set_running(False)
                    if not stopped:
                        self.progress.finish()
                    summary = f"{done} extracted, {failed} failed"
                    if stopped:
                        summary = "Stopped \u00b7 " + summary
                    self.var_status.set(summary)
                    self.write_log(summary + ".", "fail" if failed else "ok")
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
    root.title(TITLE)
    init_fonts(root)
    apply_theme(root)
    chrome = Chrome(root)
    try:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        root.iconbitmap(str(base / "app_icon.ico"))
    except Exception:
        pass
    App(root, chrome)
    root.mainloop()


if __name__ == "__main__":
    main()
