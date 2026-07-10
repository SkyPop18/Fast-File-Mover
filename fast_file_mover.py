"""
Fast File Mover - High-performance Windows file mover with node_modules exclusion.
Supports multiple source folders and files, with a toggleable node_modules skip.

Made by Akshat Sharma - https://supportme-akshat.netlify.app/
"""

from __future__ import annotations

import os
import time
import shutil
import threading
import queue
import logging
from collections import deque
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable
from datetime import datetime

import customtkinter as ctk
from tkinter import filedialog, messagebox


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class ScanResult:
    total_files: int = 0
    total_folders: int = 0
    total_size: int = 0
    largest_file: tuple[str, int] = ("", 0)
    largest_folder: tuple[str, int] = ("", 0)
    skipped_node_modules: int = 0
    skipped_files: int = 0
    skipped_size: int = 0
    avg_file_size: int = 0
    estimated_speed_mbps: float = 0.0
    estimated_seconds: float = 0.0


@dataclass
class MoveProgress:
    files_moved: int = 0
    folders_moved: int = 0
    bytes_moved: int = 0
    current_file: str = ""
    current_folder: str = ""
    skipped_node_modules: int = 0
    errors: int = 0
    speed_mbps: float = 0.0
    avg_speed_mbps: float = 0.0
    elapsed: float = 0.0
    remaining: float = 0.0
    percent: float = 0.0
    done: bool = False
    cancelled: bool = False


# ─────────────────────────────────────────────
# Logger
# ─────────────────────────────────────────────

class Logger:
    def __init__(self, log_path: Optional[Path] = None):
        self._handlers: list[Callable[[str], None]] = []
        self._error_count = 0
        self._lock = threading.Lock()
        if log_path:
            logging.basicConfig(
                filename=str(log_path),
                level=logging.WARNING,
                format="%(asctime)s %(levelname)s %(message)s",
            )

    def add_handler(self, fn: Callable[[str], None]) -> None:
        self._handlers.append(fn)

    def info(self, msg: str) -> None:
        self._emit(f"[INFO]  {msg}")

    def warn(self, msg: str) -> None:
        self._emit(f"[WARN]  {msg}")

    def error(self, msg: str) -> None:
        with self._lock:
            self._error_count += 1
        logging.warning(msg)
        self._emit(f"[ERROR] {msg}")

    def _emit(self, msg: str) -> None:
        stamped = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
        for h in self._handlers:
            try:
                h(stamped)
            except Exception:
                pass

    @property
    def error_count(self) -> int:
        with self._lock:
            return self._error_count


# ─────────────────────────────────────────────
# Scanner
# ─────────────────────────────────────────────

_SKIP = "node_modules"


class Scanner:
    def __init__(self, logger: Logger):
        self._logger = logger
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def scan(
        self,
        roots: list[Path],          # can be folders or individual files
        skip_node_modules: bool = True,
        progress_cb: Optional[Callable[[int, int, int], None]] = None,
    ) -> ScanResult:
        result = ScanResult()
        folder_sizes: dict[str, int] = {}
        _CB_INTERVAL = 500
        _cb_counter = 0
        lf_path = ""
        lf_size = 0

        # Separate files from folders up front
        dir_roots: list[str] = []
        for root in roots:
            if root.is_file():
                try:
                    sz = root.stat().st_size
                except OSError:
                    sz = 0
                result.total_files += 1
                result.total_size += sz
                if sz > lf_size:
                    lf_size = sz
                    lf_path = str(root)
            elif root.is_dir():
                dir_roots.append(str(root))

        stack: list[str] = list(dir_roots)

        while stack and not self._cancel:
            current = stack.pop()
            folder_byte_sum = 0
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if self._cancel:
                            break
                        if entry.is_dir(follow_symlinks=False):
                            if skip_node_modules and entry.name == _SKIP:
                                result.skipped_node_modules += 1
                                self._logger.info(f"Skipping node_modules: {entry.path}")
                                continue
                            result.total_folders += 1
                            stack.append(entry.path)
                        else:
                            try:
                                sz = entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                sz = 0
                            result.total_files += 1
                            result.total_size += sz
                            folder_byte_sum += sz
                            if sz > lf_size:
                                lf_size = sz
                                lf_path = entry.path
                            if progress_cb:
                                _cb_counter += 1
                                if _cb_counter >= _CB_INTERVAL:
                                    _cb_counter = 0
                                    progress_cb(result.total_files, result.total_folders, result.total_size)
            except PermissionError:
                self._logger.warn(f"Permission denied: {current}")
            if folder_byte_sum:
                folder_sizes[current] = folder_byte_sum

        result.largest_file = (lf_path, lf_size)
        if folder_sizes:
            largest_k = max(folder_sizes, key=lambda k: folder_sizes[k])
            result.largest_folder = (largest_k, folder_sizes[largest_k])
        if result.total_files:
            result.avg_file_size = result.total_size // result.total_files
        return result


# ─────────────────────────────────────────────
# Move Engine
# ─────────────────────────────────────────────

class MoveEngine:
    _COPY_BUF = 16 * 1024 * 1024
    _CB_INTERVAL_S = 0.08

    def __init__(self, logger: Logger):
        self._logger = logger
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def move(
        self,
        roots: list[Path],           # folders and/or individual files
        dest: Path,
        scan_result: ScanResult,
        skip_node_modules: bool,
        progress_cb: Callable[[MoveProgress], None],
    ) -> MoveProgress:
        prog = MoveProgress()
        total_bytes = scan_result.total_size
        start = time.perf_counter()
        speed_window: deque[tuple[float, int]] = deque()
        last_cb_time = 0.0
        is_cancelled = self._cancel_event.is_set

        def _update_and_maybe_cb() -> None:
            nonlocal last_cb_time
            now = time.perf_counter()
            cutoff = now - 3.0
            while speed_window and speed_window[0][0] < cutoff:
                speed_window.popleft()
            speed_window.append((now, prog.bytes_moved))
            if len(speed_window) >= 2:
                dt = speed_window[-1][0] - speed_window[0][0]
                db = speed_window[-1][1] - speed_window[0][1]
                prog.speed_mbps = (db / 1_048_576) / dt if dt > 0 else 0.0
            prog.elapsed = now - start
            if prog.elapsed > 0 and prog.bytes_moved > 0:
                prog.avg_speed_mbps = (prog.bytes_moved / 1_048_576) / prog.elapsed
                if prog.avg_speed_mbps > 0 and total_bytes > prog.bytes_moved:
                    prog.remaining = ((total_bytes - prog.bytes_moved) / 1_048_576) / prog.avg_speed_mbps
            if total_bytes > 0:
                prog.percent = (prog.bytes_moved / total_bytes) * 100
            if now - last_cb_time >= self._CB_INTERVAL_S:
                last_cb_time = now
                progress_cb(prog)

        dest.mkdir(parents=True, exist_ok=True)

        for root in roots:
            if is_cancelled():
                break
            if root.is_file():
                # Individual file — move directly into dest
                same_drive = self._same_drive(root, dest)
                prog.current_file = root.name
                prog.current_folder = str(root.parent)
                try:
                    file_size = root.stat().st_size
                except OSError:
                    file_size = 0
                if self._move_file(str(root), str(dest / root.name), same_drive, prog):
                    prog.files_moved += 1
                    prog.bytes_moved += file_size
                _update_and_maybe_cb()
            elif root.is_dir():
                # Directory — walk and move contents, preserving structure
                same_drive = self._same_drive(root, dest)
                root_str = str(root)
                # Destination subfolder preserves the source folder name
                dst_root = dest / root.name
                stack: list[tuple[str, Path]] = [(root_str, dst_root)]

                while stack and not is_cancelled():
                    src_dir_str, dst_dir = stack.pop()
                    try:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        self._logger.error(f"Cannot create dir {dst_dir}: {e}")
                        prog.errors += 1
                        continue
                    try:
                        with os.scandir(src_dir_str) as it:
                            entries = list(it)
                    except OSError as e:
                        self._logger.error(f"Cannot scan {src_dir_str}: {e}")
                        prog.errors += 1
                        continue

                    for entry in entries:
                        if is_cancelled():
                            break
                        if entry.is_dir(follow_symlinks=False):
                            if skip_node_modules and entry.name == _SKIP:
                                prog.skipped_node_modules += 1
                                self._logger.info(f"Skipping node_modules: {entry.path}")
                                continue
                            stack.append((entry.path, dst_dir / entry.name))
                        else:
                            prog.current_file = entry.name
                            prog.current_folder = src_dir_str
                            try:
                                file_size = entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                file_size = 0
                            if self._move_file(entry.path, str(dst_dir / entry.name), same_drive, prog):
                                prog.files_moved += 1
                                prog.bytes_moved += file_size
                            _update_and_maybe_cb()

                    if src_dir_str != root_str:
                        try:
                            os.rmdir(src_dir_str)
                            prog.folders_moved += 1
                        except OSError:
                            pass

        prog.done = True
        prog.cancelled = is_cancelled()
        prog.elapsed = time.perf_counter() - start
        progress_cb(prog)
        return prog

    def _move_file(self, src: str, dst: str, same_drive: bool, prog: MoveProgress) -> bool:
        try:
            if same_drive:
                os.replace(src, dst)
            else:
                with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, self._COPY_BUF)
                os.unlink(src)
            return True
        except PermissionError:
            self._logger.error(f"Permission denied: {src}")
        except OSError as e:
            self._logger.error(f"Failed to move {src}: {e}")
        prog.errors += 1
        return False

    @staticmethod
    def _same_drive(a: Path, b: Path) -> bool:
        try:
            return os.path.splitdrive(str(a))[0].lower() == os.path.splitdrive(str(b))[0].lower()
        except Exception:
            return False


# ─────────────────────────────────────────────
# Progress Manager
# ─────────────────────────────────────────────

class ProgressManager:
    def __init__(self, q: queue.Queue):
        self._q = q

    def push(self, prog: MoveProgress) -> None:
        try:
            self._q.put_nowait(prog)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            self._q.put_nowait(prog)


# ─────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────

class Settings:
    def __init__(self):
        # sources is a list of Path objects (files and/or folders)
        self.sources: list[Path] = []
        self.dest: Optional[Path] = None
        self.scan_result: Optional[ScanResult] = None
        self.skip_node_modules: bool = True


# ─────────────────────────────────────────────
# Colour tokens
# ─────────────────────────────────────────────

_C = {
    "bg":       "#0d0d14",
    "surface":  "#13131f",
    "raised":   "#1a1a2e",
    "border":   "#252540",
    "input":    "#1e1e32",
    "green":    "#22c55e",
    "green_d":  "#16a34a",
    "blue":     "#3b82f6",
    "blue_d":   "#2563eb",
    "red":      "#ef4444",
    "red_d":    "#b91c1c",
    "amber":    "#f59e0b",
    "muted":    "#4b5563",
    "subtle":   "#374151",
    "dim":      "#6b7280",
    "text":     "#e2e8f0",
    "text_dim": "#94a3b8",
    "tag_folder": "#1e3a5f",
    "tag_file":   "#2d1b4e",
}


# ─────────────────────────────────────────────
# Source list widget
# ─────────────────────────────────────────────

class SourceList(ctk.CTkScrollableFrame):
    """Scrollable list showing added source folders/files with remove buttons."""

    ICON_FOLDER = "📁"
    ICON_FILE   = "📄"

    def __init__(self, master, on_change: Callable[[], None], **kwargs):
        super().__init__(master, **kwargs)
        self._on_change = on_change
        self._rows: list[tuple[Path, ctk.CTkFrame]] = []  # (path, row_frame)
        self.grid_columnconfigure(0, weight=1)

    def add(self, path: Path) -> bool:
        """Add a path. Returns False if already present."""
        if any(p == path for p, _ in self._rows):
            return False
        row_idx = len(self._rows)
        frame = ctk.CTkFrame(self, fg_color=_C["raised"], corner_radius=6)
        frame.grid(row=row_idx, column=0, padx=4, pady=3, sticky="ew")
        frame.grid_columnconfigure(1, weight=1)

        is_dir = path.is_dir()
        icon = self.ICON_FOLDER if is_dir else self.ICON_FILE
        tag_color = _C["tag_folder"] if is_dir else _C["tag_file"]

        tag = ctk.CTkFrame(frame, fg_color=tag_color, corner_radius=4, width=28, height=22)
        tag.grid(row=0, column=0, padx=(8, 6), pady=6)
        tag.grid_propagate(False)
        ctk.CTkLabel(tag, text=icon, font=ctk.CTkFont(size=11)).place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(
            frame, text=str(path),
            font=ctk.CTkFont(size=11),
            text_color=_C["text"], anchor="w",
            wraplength=480,
        ).grid(row=0, column=1, padx=(0, 6), pady=6, sticky="ew")

        def _remove(p=path):
            self._remove(p)

        ctk.CTkButton(
            frame, text="✕", width=26, height=26,
            fg_color=_C["subtle"], hover_color=_C["red_d"],
            font=ctk.CTkFont(size=11), corner_radius=4,
            command=_remove,
        ).grid(row=0, column=2, padx=(0, 8), pady=6)

        self._rows.append((path, frame))
        self._on_change()
        return True

    def _remove(self, path: Path) -> None:
        self._rows = [(p, f) for p, f in self._rows if p != path]
        # Destroy and re-grid all rows cleanly
        for widget in self.winfo_children():
            widget.destroy()
        self._rows_rebuilt: list[tuple[Path, ctk.CTkFrame]] = []
        for p, _ in list(self._rows):
            # Re-add without triggering on_change again
            self._rows = []
        # Easiest: just rebuild from scratch stored paths
        saved = [p for p, _ in list(self._rows)]
        # Since we already cleared self._rows, rebuild:
        # Actually: collect before clearing
        pass
        self._on_change()

    # Cleaner remove: rebuild all rows from scratch
    def remove(self, path: Path) -> None:
        remaining = [p for p, _ in self._rows if p != path]
        for widget in self.winfo_children():
            widget.destroy()
        self._rows = []
        for p in remaining:
            self.add(p)

    def _remove(self, path: Path) -> None:  # noqa: F811  (intentional override)
        self.remove(path)

    def paths(self) -> list[Path]:
        return [p for p, _ in self._rows]

    def clear(self) -> None:
        for widget in self.winfo_children():
            widget.destroy()
        self._rows = []
        self._on_change()

    def count(self) -> int:
        return len(self._rows)


# ─────────────────────────────────────────────
# Main App
# ─────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("⚡ Fast File Mover")
        self.geometry("1000x900")
        self.minsize(840, 740)
        self.configure(fg_color=_C["bg"])

        self._settings = Settings()
        self._logger = Logger(Path.home() / "fast_file_mover_errors.log")
        self._logger.add_handler(self._append_log)

        self._scanner = Scanner(self._logger)
        self._engine: Optional[MoveEngine] = None
        self._prog_queue: queue.Queue = queue.Queue(maxsize=50)
        self._prog_manager = ProgressManager(self._prog_queue)

        self._scan_thread: Optional[threading.Thread] = None
        self._move_thread: Optional[threading.Thread] = None
        self._moving = False
        self._stat_labels: dict[str, ctk.CTkLabel] = {}

        self._build_ui()
        self._poll_progress()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # The window itself is just a host for one scrollable frame that
        # contains everything. This means the user can always scroll to see
        # content that doesn't fit the current window height.
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._scroll = ctk.CTkScrollableFrame(
            self,
            fg_color=_C["bg"],
            scrollbar_button_color=_C["border"],
            scrollbar_button_hover_color=_C["subtle"],
        )
        self._scroll.grid(row=0, column=0, sticky="nsew")
        self._scroll.grid_columnconfigure(0, weight=1)

        # All sections go into self._scroll, not self directly
        self._build_header()   # row 0
        self._build_pickers()  # row 1
        self._build_stats()    # row 2
        self._build_log()      # row 3
        self._build_footer()   # row 4

    # ── Header ───────────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self._scroll, fg_color=_C["surface"], corner_radius=12)
        hdr.grid(row=0, column=0, padx=12, pady=(12, 4), sticky="ew")
        hdr.grid_columnconfigure(1, weight=1)

        title_f = ctk.CTkFrame(hdr, fg_color="transparent")
        title_f.grid(row=0, column=0, padx=18, pady=14, sticky="w")
        ctk.CTkLabel(title_f, text="⚡", font=ctk.CTkFont(size=26), text_color=_C["green"]).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkLabel(title_f, text="Fast File Mover", font=ctk.CTkFont(size=20, weight="bold"), text_color=_C["text"]).grid(row=0, column=1)

        ctk.CTkLabel(
            hdr, text="Multi-source transfer  ·  node_modules skip toggle",
            font=ctk.CTkFont(size=12), text_color=_C["dim"],
        ).grid(row=0, column=1, padx=8, pady=14, sticky="w")

    # ── Pickers ──────────────────────────────────────────────────────────────

    def _build_pickers(self) -> None:
        outer = ctk.CTkFrame(self._scroll, fg_color=_C["surface"], corner_radius=12)
        outer.grid(row=1, column=0, padx=12, pady=4, sticky="ew")
        outer.grid_columnconfigure(0, weight=1)

        # ── SOURCE section ────────────────────────────────────────────────────
        src_header = ctk.CTkFrame(outer, fg_color="transparent")
        src_header.grid(row=0, column=0, padx=14, pady=(12, 4), sticky="ew")
        src_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            src_header, text="SOURCE",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=_C["dim"],
        ).grid(row=0, column=0, sticky="w")

        # Add-buttons row
        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.grid(row=1, column=0, padx=14, pady=(0, 6), sticky="ew")

        ctk.CTkButton(
            btn_row, text="📁  Add Folder",
            command=self._add_source_folder,
            fg_color=_C["subtle"], hover_color=_C["muted"],
            font=ctk.CTkFont(size=12), width=130, height=32, corner_radius=7,
        ).grid(row=0, column=0, padx=(0, 8))

        ctk.CTkButton(
            btn_row, text="📄  Add Files",
            command=self._add_source_files,
            fg_color=_C["subtle"], hover_color=_C["muted"],
            font=ctk.CTkFont(size=12), width=120, height=32, corner_radius=7,
        ).grid(row=0, column=1, padx=(0, 8))

        ctk.CTkButton(
            btn_row, text="🗑  Clear All",
            command=self._clear_sources,
            fg_color=_C["raised"], hover_color=_C["red_d"],
            font=ctk.CTkFont(size=12), width=110, height=32, corner_radius=7,
        ).grid(row=0, column=2)

        # Source list (scrollable)
        self._source_list = SourceList(
            outer,
            on_change=self._on_sources_changed,
            fg_color=_C["input"],
            corner_radius=8,
            height=90,
            scrollbar_button_color=_C["border"],
            scrollbar_button_hover_color=_C["subtle"],
        )
        self._source_list.grid(row=2, column=0, padx=14, pady=(0, 8), sticky="ew")

        self._src_count_lbl = ctk.CTkLabel(
            outer, text="No sources added",
            font=ctk.CTkFont(size=11), text_color=_C["dim"], anchor="w",
        )
        self._src_count_lbl.grid(row=3, column=0, padx=16, pady=(0, 4), sticky="w")

        # ── Separator ─────────────────────────────────────────────────────────
        ctk.CTkFrame(outer, height=1, fg_color=_C["border"]).grid(
            row=4, column=0, padx=14, pady=4, sticky="ew"
        )

        # ── DESTINATION row ───────────────────────────────────────────────────
        dst_header = ctk.CTkFrame(outer, fg_color="transparent")
        dst_header.grid(row=5, column=0, padx=14, pady=(6, 4), sticky="ew")
        ctk.CTkLabel(
            dst_header, text="DESTINATION",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=_C["dim"],
        ).grid(row=0, column=0, sticky="w")

        dst_row = ctk.CTkFrame(outer, fg_color="transparent")
        dst_row.grid(row=6, column=0, padx=14, pady=(0, 6), sticky="ew")
        dst_row.grid_columnconfigure(0, weight=1)

        self._dst_var = ctk.StringVar()
        ctk.CTkEntry(
            dst_row, textvariable=self._dst_var,
            state="readonly",
            font=ctk.CTkFont(size=12),
            fg_color=_C["input"], border_color=_C["border"],
            text_color=_C["text"], height=34,
        ).grid(row=0, column=0, padx=(0, 8), sticky="ew")

        ctk.CTkButton(
            dst_row, text="📁  Browse",
            command=self._pick_dest,
            fg_color=_C["subtle"], hover_color=_C["muted"],
            font=ctk.CTkFont(size=12), width=110, height=34, corner_radius=7,
        ).grid(row=0, column=1)

        # ── Options row (skip node_modules checkbox) ───────────────────────────
        opts_row = ctk.CTkFrame(outer, fg_color="transparent")
        opts_row.grid(row=7, column=0, padx=14, pady=(2, 10), sticky="ew")

        self._skip_nm_var = ctk.BooleanVar(value=True)
        self._skip_nm_cb = ctk.CTkCheckBox(
            opts_row,
            text="Skip node_modules directories",
            variable=self._skip_nm_var,
            onvalue=True, offvalue=False,
            font=ctk.CTkFont(size=12),
            text_color=_C["text"],
            fg_color=_C["green"],
            hover_color=_C["green_d"],
            checkmark_color="#ffffff",
            border_color=_C["border"],
            command=self._on_skip_toggle,
        )
        self._skip_nm_cb.grid(row=0, column=0, sticky="w")

        self._skip_nm_badge = ctk.CTkLabel(
            opts_row,
            text="● ACTIVE",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=_C["green"],
        )
        self._skip_nm_badge.grid(row=0, column=1, padx=(10, 0), sticky="w")

        # ── Action buttons ─────────────────────────────────────────────────────
        ctk.CTkFrame(outer, height=1, fg_color=_C["border"]).grid(
            row=8, column=0, padx=14, pady=(0, 8), sticky="ew"
        )
        action_row = ctk.CTkFrame(outer, fg_color="transparent")
        action_row.grid(row=9, column=0, padx=14, pady=(0, 14), sticky="w")

        self._scan_btn = ctk.CTkButton(
            action_row, text="🔍  Scan",
            command=self._start_scan,
            fg_color=_C["blue"], hover_color=_C["blue_d"],
            font=ctk.CTkFont(size=13, weight="bold"),
            width=120, height=36, corner_radius=8,
        )
        self._scan_btn.grid(row=0, column=0, padx=(0, 8))

        self._move_btn = ctk.CTkButton(
            action_row, text="🚀  Move Files",
            command=self._start_move,
            fg_color=_C["green"], hover_color=_C["green_d"],
            font=ctk.CTkFont(size=13, weight="bold"),
            state="disabled", width=140, height=36, corner_radius=8,
        )
        self._move_btn.grid(row=0, column=1, padx=(0, 8))

        self._cancel_btn = ctk.CTkButton(
            action_row, text="✖  Cancel",
            command=self._cancel,
            fg_color=_C["red"], hover_color=_C["red_d"],
            font=ctk.CTkFont(size=13, weight="bold"),
            state="disabled", width=110, height=36, corner_radius=8,
        )
        self._cancel_btn.grid(row=0, column=2)

    # ── Stats panel ──────────────────────────────────────────────────────────

    def _build_stats(self) -> None:
        outer = ctk.CTkFrame(self._scroll, fg_color=_C["surface"], corner_radius=12)
        outer.grid(row=2, column=0, padx=12, pady=4, sticky="ew")
        outer.grid_columnconfigure(0, weight=1)

        cards_frame = ctk.CTkFrame(outer, fg_color="transparent")
        cards_frame.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="ew")
        cards_frame.grid_columnconfigure((0, 1, 2), weight=1)

        scan_card = self._make_card(cards_frame, "📊  Scan Results", 0)
        for i, (key, lbl) in enumerate([
            ("files",          "Files"),
            ("folders",        "Folders"),
            ("size",           "Total Size"),
            ("largest_file",   "Largest File"),
            ("largest_folder", "Largest Folder"),
            ("avg_size",       "Avg File Size"),
            ("skipped_nm",     "Skipped node_modules"),
            ("skipped_files",  "Skipped Files"),
            ("skipped_size",   "Skipped Size"),
        ]):
            self._make_stat_row(scan_card, i, lbl, key)

        est_card = self._make_card(cards_frame, "⏱  Transfer Estimate", 1)
        for i, (key, lbl) in enumerate([
            ("est_speed", "Est. Speed"),
            ("est_time",  "Est. Time"),
        ]):
            self._make_stat_row(est_card, i, lbl, key)

        live_card = self._make_card(cards_frame, "📡  Live Transfer", 2)
        for i, (key, lbl) in enumerate([
            ("cur_file",      "Current File"),
            ("files_moved",   "Files Moved"),
            ("folders_moved", "Folders Moved"),
            ("speed",         "Speed"),
            ("avg_speed",     "Avg Speed"),
            ("elapsed",       "Elapsed"),
            ("remaining",     "Remaining"),
            ("percent",       "Progress"),
        ]):
            self._make_stat_row(live_card, i, lbl, key)

        # Progress bar panel
        pb_outer = ctk.CTkFrame(outer, fg_color=_C["raised"], corner_radius=10)
        pb_outer.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")
        pb_outer.grid_columnconfigure(0, weight=1)

        pb_hdr = ctk.CTkFrame(pb_outer, fg_color="transparent")
        pb_hdr.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        pb_hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(pb_hdr, text="Transfer Progress", font=ctk.CTkFont(size=11, weight="bold"), text_color=_C["text_dim"]).grid(row=0, column=0, sticky="w")
        self._pct_label = ctk.CTkLabel(pb_hdr, text="0.0%", font=ctk.CTkFont(size=11, weight="bold"), text_color=_C["green"])
        self._pct_label.grid(row=0, column=1, sticky="e")

        self._progress = ctk.CTkProgressBar(pb_outer, height=16, corner_radius=8, progress_color=_C["green"], fg_color=_C["border"])
        self._progress.set(0)
        self._progress.grid(row=1, column=0, padx=14, pady=(0, 12), sticky="ew")

    def _make_card(self, parent, title: str, col: int) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=_C["raised"], corner_radius=10)
        card.grid(row=0, column=col, padx=4, pady=4, sticky="nsew")
        card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=11, weight="bold"), text_color=_C["text_dim"]).grid(
            row=0, column=0, columnspan=2, padx=12, pady=(10, 4), sticky="w"
        )
        ctk.CTkFrame(card, height=1, fg_color=_C["border"]).grid(row=1, column=0, columnspan=2, padx=8, pady=(0, 4), sticky="ew")
        return card

    def _make_stat_row(self, card, row: int, label: str, key: str) -> None:
        r = row + 2
        ctk.CTkLabel(card, text=label + ":", font=ctk.CTkFont(size=11), text_color=_C["dim"], anchor="w", width=120).grid(
            row=r, column=0, padx=(12, 4), pady=2, sticky="w"
        )
        val = ctk.CTkLabel(card, text="—", font=ctk.CTkFont(size=11), text_color=_C["text"], anchor="w")
        val.grid(row=r, column=1, padx=(0, 12), pady=2, sticky="w")
        self._stat_labels[key] = val

    # ── Log ──────────────────────────────────────────────────────────────────

    def _build_log(self) -> None:
        frame = ctk.CTkFrame(self._scroll, fg_color=_C["surface"], corner_radius=12)
        frame.grid(row=3, column=0, padx=12, pady=4, sticky="ew")
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame, text="📋  Live Log", font=ctk.CTkFont(size=11, weight="bold"), text_color=_C["text_dim"]).grid(
            row=0, column=0, padx=14, pady=(10, 4), sticky="w"
        )
        self._log_box = ctk.CTkTextbox(
            frame, font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#080810", text_color="#4ade80",
            corner_radius=8, wrap="none",
            border_width=1, border_color=_C["border"],
            height=160,
        )
        self._log_box.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")

    # ── Footer ───────────────────────────────────────────────────────────────

    def _build_footer(self) -> None:
        frame = ctk.CTkFrame(self._scroll, fg_color="transparent")
        frame.grid(row=4, column=0, padx=12, pady=(0, 10), sticky="ew")
        frame.grid_columnconfigure(0, weight=1)
        self._status_lbl = ctk.CTkLabel(frame, text="Ready", font=ctk.CTkFont(size=12), text_color=_C["dim"], anchor="w")
        self._status_lbl.grid(row=0, column=0, sticky="w")

    # ── Source management ────────────────────────────────────────────────────

    def _add_source_folder(self) -> None:
        path = filedialog.askdirectory(title="Add Source Folder")
        if not path:
            return
        p = Path(path)
        if not self._source_list.add(p):
            self._set_status(f"Already added: {p.name}")

    def _add_source_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Add Source Files")
        added = 0
        for path in paths:
            if self._source_list.add(Path(path)):
                added += 1
        if added:
            self._set_status(f"Added {added} file(s).")

    def _clear_sources(self) -> None:
        self._source_list.clear()

    def _on_sources_changed(self) -> None:
        count = self._source_list.count()
        if count == 0:
            self._src_count_lbl.configure(text="No sources added")
        else:
            folders = sum(1 for p in self._source_list.paths() if p.is_dir())
            files = count - folders
            parts = []
            if folders:
                parts.append(f"{folders} folder{'s' if folders != 1 else ''}")
            if files:
                parts.append(f"{files} file{'s' if files != 1 else ''}")
            self._src_count_lbl.configure(text="Sources: " + "  +  ".join(parts))
        # Reset scan when sources change
        self._settings.scan_result = None
        self._move_btn.configure(state="disabled")

    def _on_skip_toggle(self) -> None:
        active = self._skip_nm_var.get()
        self._settings.skip_node_modules = active
        if active:
            self._skip_nm_badge.configure(text="● ACTIVE", text_color=_C["green"])
        else:
            self._skip_nm_badge.configure(text="○ INACTIVE", text_color=_C["amber"])
        # Reset scan result when option changes
        self._settings.scan_result = None
        self._move_btn.configure(state="disabled")

    def _pick_dest(self) -> None:
        path = filedialog.askdirectory(title="Select Destination Folder")
        if path:
            self._settings.dest = Path(path)
            self._dst_var.set(path)
            self._settings.scan_result = None
            self._move_btn.configure(state="disabled")
            self._set_status("Destination set.")

    # ── Validation ───────────────────────────────────────────────────────────

    def _validate(self) -> bool:
        sources = self._source_list.paths()
        dst = self._settings.dest
        if not sources:
            messagebox.showerror("Error", "Please add at least one source folder or file.")
            return False
        if not dst:
            messagebox.showerror("Error", "Please select a destination folder.")
            return False
        for src in sources:
            if not src.exists():
                messagebox.showerror("Error", f"Source does not exist:\n{src}")
                return False
            if src == dst:
                messagebox.showerror("Error", f"Source and destination cannot be the same:\n{src}")
                return False
            if src.is_dir():
                try:
                    dst.relative_to(src)
                    messagebox.showerror("Error", f"Destination cannot be inside source:\n{src}")
                    return False
                except ValueError:
                    pass
        return True

    # ── Scan ─────────────────────────────────────────────────────────────────

    def _start_scan(self) -> None:
        if not self._validate():
            return
        sources = self._source_list.paths()
        self._settings.sources = sources
        self._settings.skip_node_modules = self._skip_nm_var.get()

        self._scan_btn.configure(state="disabled")
        self._move_btn.configure(state="disabled")
        self._set_status("Scanning…")
        self._scanner = Scanner(self._logger)

        def _run() -> None:
            result = self._scanner.scan(
                self._settings.sources,
                skip_node_modules=self._settings.skip_node_modules,
                progress_cb=lambda f, d, s: self.after(0, self._on_scan_progress, f, d, s),
            )
            self.after(0, self._on_scan_done, result)

        self._scan_thread = threading.Thread(target=_run, daemon=True)
        self._scan_thread.start()

    def _on_scan_progress(self, files: int, folders: int, size: int) -> None:
        self._set_status(f"Scanning… {files:,} files · {folders:,} folders · {_fmt_size(size)}")

    def _on_scan_done(self, result: ScanResult) -> None:
        self._settings.scan_result = result
        r = result

        self._set_stat("files",  f"{r.total_files:,}")
        self._set_stat("folders", f"{r.total_folders:,}")
        self._set_stat("size",   _fmt_size(r.total_size))
        self._set_stat("largest_file",
            f"{os.path.basename(r.largest_file[0])} ({_fmt_size(r.largest_file[1])})"
            if r.largest_file[0] else "—")
        self._set_stat("largest_folder",
            f"{os.path.basename(r.largest_folder[0])} ({_fmt_size(r.largest_folder[1])})"
            if r.largest_folder[0] else "—")
        self._set_stat("avg_size",     _fmt_size(r.avg_file_size))
        self._set_stat("skipped_nm",   str(r.skipped_node_modules))
        self._set_stat("skipped_files","0")
        self._set_stat("skipped_size", "0")
        self._set_stat("est_speed",    "— (live during transfer)")
        self._set_stat("est_time",     "— (live during transfer)")

        self._scan_btn.configure(state="normal")
        self._move_btn.configure(state="normal")
        self._set_status(
            f"Scan complete — {r.total_files:,} files, {r.total_folders:,} folders, {_fmt_size(r.total_size)}"
        )

    # ── Move ─────────────────────────────────────────────────────────────────

    def _start_move(self) -> None:
        if self._moving:
            return
        if not self._validate():
            return
        if not self._settings.scan_result:
            messagebox.showerror("Error", "Please scan first.")
            return

        dst = self._settings.dest
        try:
            dst.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Error", f"Cannot create destination:\n{e}")
            return

        n = self._settings.scan_result.total_files
        if not messagebox.askyesno("Confirm Move", f"Move {n:,} files to:\n{dst}\n\nProceed?"):
            return

        self._moving = True
        self._engine = MoveEngine(self._logger)
        self._scan_btn.configure(state="disabled")
        self._move_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._progress.set(0)
        self._pct_label.configure(text="0.0%")
        self._set_status("Moving files…")

        sources = list(self._settings.sources)
        skip_nm = self._settings.skip_node_modules

        def _run() -> None:
            result = self._engine.move(
                sources, dst, self._settings.scan_result, skip_nm,
                lambda p: self._prog_manager.push(p),
            )
            self.after(0, self._on_move_done, result)

        self._move_thread = threading.Thread(target=_run, daemon=True)
        self._move_thread.start()

    def _on_move_done(self, prog: MoveProgress) -> None:
        self._moving = False
        self._cancel_btn.configure(state="disabled")
        self._scan_btn.configure(state="normal")

        if prog.cancelled:
            self._set_status("Transfer cancelled.")
            return

        msg = (
            f"✅  Transfer complete!\n\n"
            f"Files moved:          {prog.files_moved:,}\n"
            f"Folders moved:        {prog.folders_moved:,}\n"
            f"Skipped node_modules: {prog.skipped_node_modules}\n"
            f"Elapsed time:         {_fmt_time(prog.elapsed)}\n"
            f"Average speed:        {prog.avg_speed_mbps:.1f} MB/s\n"
            f"Errors:               {prog.errors}"
        )
        if prog.errors:
            msg += f"\n\nError log: {Path.home() / 'fast_file_mover_errors.log'}"

        if messagebox.askyesno("Done", msg + "\n\nOpen destination folder?"):
            os.startfile(str(self._settings.dest))

        self._set_status(f"Done — {prog.files_moved:,} files in {_fmt_time(prog.elapsed)}")

    # ── Cancel ───────────────────────────────────────────────────────────────

    def _cancel(self) -> None:
        if self._engine:
            self._engine.cancel()
        if self._scanner:
            self._scanner.cancel()
        self._cancel_btn.configure(state="disabled")
        self._set_status("Cancelling…")

    # ── Progress polling ─────────────────────────────────────────────────────

    def _poll_progress(self) -> None:
        try:
            while True:
                prog: MoveProgress = self._prog_queue.get_nowait()
                self._apply_progress(prog)
        except queue.Empty:
            pass
        self.after(80, self._poll_progress)

    def _apply_progress(self, prog: MoveProgress) -> None:
        self._progress.set(min(prog.percent / 100.0, 1.0))
        self._pct_label.configure(text=f"{prog.percent:.1f}%")
        self._set_stat("cur_file",      prog.current_file[:55] if prog.current_file else "—")
        self._set_stat("files_moved",   f"{prog.files_moved:,}")
        self._set_stat("folders_moved", f"{prog.folders_moved:,}")
        self._set_stat("speed",         f"{prog.speed_mbps:.1f} MB/s")
        self._set_stat("avg_speed",     f"{prog.avg_speed_mbps:.1f} MB/s")
        self._set_stat("elapsed",       _fmt_time(prog.elapsed))
        self._set_stat("remaining",     _fmt_time(prog.remaining))
        self._set_stat("percent",       f"{prog.percent:.1f}%")
        if prog.avg_speed_mbps > 0:
            self._set_stat("est_speed", f"{prog.avg_speed_mbps:.1f} MB/s")
            self._set_stat("est_time",  _fmt_time(prog.remaining))

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _set_stat(self, key: str, value: str) -> None:
        lbl = self._stat_labels.get(key)
        if lbl:
            lbl.configure(text=value)

    def _set_status(self, msg: str) -> None:
        self._status_lbl.configure(text=msg)

    def _append_log(self, msg: str) -> None:
        def _do() -> None:
            self._log_box.insert("end", msg + "\n")
            self._log_box.see("end")
        self.after(0, _do)


# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_time(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()