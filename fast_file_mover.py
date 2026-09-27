"""
Fast File Mover & Copier - High-performance Windows file transfer utility.
Supports Drag & Drop, Move & Copy modes, duplicate resolution (Ask/Skip/Replace/Rename),
node_modules exclusion, and comprehensive error handling.

Made by Akshat Sharma - https://supportme-akshat.netlify.app/
"""

from __future__ import annotations

import os
import sys
import time
import uuid
import stat
import shutil
import threading
import queue
import logging
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Callable
from datetime import datetime

# Ensure the directory of this script is in sys.path for bundled packages
_script_dir = str(Path(__file__).resolve().parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

import customtkinter as ctk
from tkinter import filedialog, messagebox

# Safe Drag and Drop integration via TkinterDnD
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    _HAS_DND = False


# ─────────────────────────────────────────────
# Base Window Setup
# ─────────────────────────────────────────────

if _HAS_DND:
    class _BaseApp(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    class _BaseApp(ctk.CTk):
        pass


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
    dest_free_space: int = 0


@dataclass
class TransferProgress:
    files_transferred: int = 0
    folders_affected: int = 0
    bytes_transferred: int = 0
    bytes_skipped: int = 0
    current_file: str = ""
    current_folder: str = ""
    skipped_node_modules: int = 0
    skipped_duplicates: int = 0
    renamed_files: int = 0
    errors: int = 0
    failed_files: list[tuple[str, str]] = field(default_factory=list)
    speed_mbps: float = 0.0
    avg_speed_mbps: float = 0.0
    elapsed: float = 0.0
    remaining: float = 0.0
    percent: float = 0.0
    done: bool = False
    cancelled: bool = False
    mode: str = "move"


# Backwards compatibility alias
MoveProgress = TransferProgress


# ─────────────────────────────────────────────
# Logger
# ─────────────────────────────────────────────

class Logger:
    def __init__(self, log_path: Optional[Path] = None):
        self._handlers: list[Callable[[str], None]] = []
        self._error_count = 0
        self._lock = threading.Lock()
        self.log_path = log_path
        if log_path:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                logging.basicConfig(
                    filename=str(log_path),
                    level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(message)s",
                )
            except Exception:
                pass

    def add_handler(self, fn: Callable[[str], None]) -> None:
        self._handlers.append(fn)

    def info(self, msg: str) -> None:
        self._emit(f"[INFO]  {msg}")

    def warn(self, msg: str) -> None:
        self._emit(f"[WARN]  {msg}")

    def error(self, msg: str) -> None:
        with self._lock:
            self._error_count += 1
        try:
            logging.warning(msg)
        except Exception:
            pass
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
        roots: list[Path],          # folders or individual files
        dest: Optional[Path] = None,
        skip_node_modules: bool = True,
        progress_cb: Optional[Callable[[int, int, int], None]] = None,
    ) -> ScanResult:
        result = ScanResult()
        folder_sizes: dict[str, int] = {}
        _CB_INTERVAL = 400
        _cb_counter = 0
        lf_path = ""
        lf_size = 0

        if dest:
            result.dest_free_space = _get_free_space(dest)

        dir_roots: list[str] = []
        for root in roots:
            if not root.exists():
                continue
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
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue

                        if is_dir:
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
            except OSError as e:
                self._logger.warn(f"Error scanning {current}: {e}")

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
# Transfer Engine (Move & Copy)
# ─────────────────────────────────────────────

class TransferEngine:
    _CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks for smooth progress and instant cancel
    _CB_INTERVAL_S = 0.08

    def __init__(self, logger: Logger, root_window: Optional[ctk.CTk] = None):
        self._logger = logger
        self._root = root_window
        self._cancel_event = threading.Event()
        self._apply_all_conflict: Optional[str] = None
        self._apply_all_error: Optional[str] = None

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run_transfer(
        self,
        roots: list[Path],
        dest: Path,
        scan_result: ScanResult,
        mode: str,                  # "move" or "copy"
        conflict_policy: str,       # "ask", "skip", "replace", "rename"
        skip_node_modules: bool,
        preserve_timestamps: bool,
        progress_cb: Callable[[TransferProgress], None],
    ) -> TransferProgress:
        prog = TransferProgress()
        prog.mode = mode
        total_bytes = scan_result.total_size
        start = time.perf_counter()
        speed_window: deque[tuple[float, int]] = deque()
        last_cb_time = 0.0

        def _update_and_maybe_cb(force: bool = False) -> None:
            nonlocal last_cb_time
            now = time.perf_counter()
            cutoff = now - 3.0
            while speed_window and speed_window[0][0] < cutoff:
                speed_window.popleft()
            speed_window.append((now, prog.bytes_transferred))
            if len(speed_window) >= 2:
                dt = speed_window[-1][0] - speed_window[0][0]
                db = speed_window[-1][1] - speed_window[0][1]
                prog.speed_mbps = (db / 1_048_576) / dt if dt > 0 else 0.0
            prog.elapsed = now - start
            bytes_processed = prog.bytes_transferred + prog.bytes_skipped
            if prog.elapsed > 0 and prog.bytes_transferred > 0:
                prog.avg_speed_mbps = (prog.bytes_transferred / 1_048_576) / prog.elapsed
                bytes_left = max(0, total_bytes - bytes_processed)
                if prog.avg_speed_mbps > 0 and bytes_left > 0:
                    prog.remaining = (bytes_left / 1_048_576) / prog.avg_speed_mbps
                else:
                    prog.remaining = 0.0
            else:
                prog.remaining = 0.0

            if total_bytes > 0:
                prog.percent = min((bytes_processed / total_bytes) * 100.0, 100.0)
            elif prog.done:
                prog.percent = 100.0

            if force or (now - last_cb_time >= self._CB_INTERVAL_S):
                last_cb_time = now
                progress_cb(prog)

        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._logger.error(f"Cannot create destination {dest}: {e}")
            prog.errors += 1
            prog.done = True
            progress_cb(prog)
            return prog

        for root in roots:
            if self.is_cancelled():
                break

            if not root.exists():
                self._logger.warn(f"Source path not found: {root}")
                continue

            if root.is_file():
                target_file = dest / root.name
                prog.current_file = root.name
                prog.current_folder = str(root.parent)
                _update_and_maybe_cb()

                self._process_single_file(
                    src=root,
                    dst=target_file,
                    mode=mode,
                    conflict_policy=conflict_policy,
                    preserve_timestamps=preserve_timestamps,
                    prog=prog,
                    update_cb=_update_and_maybe_cb,
                )

            elif root.is_dir():
                dst_root = dest / root.name
                root_str = str(root)
                stack: list[tuple[str, Path]] = [(root_str, dst_root)]
                dirs_to_clean: list[str] = []

                while stack and not self.is_cancelled():
                    src_dir_str, dst_dir = stack.pop()
                    if mode == "move" and src_dir_str != root_str:
                        dirs_to_clean.append(src_dir_str)

                    try:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        self._logger.error(f"Cannot create directory {dst_dir}: {e}")
                        prog.errors += 1
                        continue

                    try:
                        with os.scandir(src_dir_str) as it:
                            entries = list(it)
                    except OSError as e:
                        self._logger.error(f"Cannot scan directory {src_dir_str}: {e}")
                        prog.errors += 1
                        continue

                    for entry in entries:
                        if self.is_cancelled():
                            break

                        try:
                            is_entry_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue

                        if is_entry_dir:
                            if skip_node_modules and entry.name == _SKIP:
                                prog.skipped_node_modules += 1
                                self._logger.info(f"Skipping node_modules: {entry.path}")
                                continue
                            stack.append((entry.path, dst_dir / entry.name))
                        else:
                            prog.current_file = entry.name
                            prog.current_folder = src_dir_str
                            _update_and_maybe_cb()

                            src_file = Path(entry.path)
                            target_file = dst_dir / entry.name

                            self._process_single_file(
                                src=src_file,
                                dst=target_file,
                                mode=mode,
                                conflict_policy=conflict_policy,
                                preserve_timestamps=preserve_timestamps,
                                prog=prog,
                                update_cb=_update_and_maybe_cb,
                            )

                if mode == "move" and not self.is_cancelled():
                    dirs_to_clean.sort(key=lambda p: len(p), reverse=True)
                    for d in dirs_to_clean:
                        try:
                            os.rmdir(d)
                            prog.folders_affected += 1
                        except OSError:
                            pass
                    try:
                        os.rmdir(root_str)
                        prog.folders_affected += 1
                    except OSError:
                        pass

        prog.done = True
        prog.cancelled = self.is_cancelled()
        prog.elapsed = time.perf_counter() - start
        if not prog.cancelled:
            prog.percent = 100.0
            prog.remaining = 0.0
            prog.speed_mbps = 0.0
            prog.current_file = "All files processed"
        _update_and_maybe_cb(force=True)
        return prog

    # Backwards compatibility alias
    move = run_transfer

    # ── Single file processing & conflict resolution ─────────────────────────

    def _process_single_file(
        self,
        src: Path,
        dst: Path,
        mode: str,
        conflict_policy: str,
        preserve_timestamps: bool,
        prog: TransferProgress,
        update_cb: Callable[[], None],
    ) -> bool:
        if self.is_cancelled():
            return False

        try:
            file_size = src.stat().st_size
        except OSError as e:
            self._logger.error(f"Cannot read file info for {src}: {e}")
            prog.errors += 1
            prog.failed_files.append((str(src), str(e)))
            return False

        final_dst = dst

        if final_dst.exists():
            action = self._resolve_conflict(src, final_dst, conflict_policy, mode)

            if action == "cancel":
                self._cancel_event.set()
                return False

            if action == "skip":
                prog.skipped_duplicates += 1
                prog.bytes_skipped += file_size
                prog.current_file = f"Skipped: {src.name}"
                self._logger.info(f"Skipped duplicate: {src.name}")
                update_cb()
                return False

            if action == "rename":
                final_dst = self._generate_unique_name(final_dst)
                prog.renamed_files += 1
                self._logger.info(f"Auto-renamed duplicate {src.name} -> {final_dst.name}")

            elif action == "replace":
                self._logger.info(f"Replacing existing file: {final_dst.name}")
                _clear_readonly(final_dst)

        while not self.is_cancelled():
            try:
                success = self._transfer_bytes(
                    src=src,
                    dst=final_dst,
                    file_size=file_size,
                    mode=mode,
                    preserve_timestamps=preserve_timestamps,
                    prog=prog,
                    update_cb=update_cb,
                )
                if success:
                    prog.files_transferred += 1
                    return True
                else:
                    return False
            except Exception as exc:
                if self.is_cancelled():
                    return False
                action = self._resolve_error(src, exc, mode)
                if action == "retry":
                    self._logger.info(f"Retrying transfer of {src.name}...")
                    continue
                elif action == "skip":
                    prog.errors += 1
                    prog.bytes_skipped += file_size
                    prog.failed_files.append((str(src), str(exc)))
                    self._logger.error(f"Skipped failed file {src}: {exc}")
                    update_cb()
                    return False
                elif action == "abort":
                    self._cancel_event.set()
                    return False

        return False

    def _transfer_bytes(
        self,
        src: Path,
        dst: Path,
        file_size: int,
        mode: str,
        preserve_timestamps: bool,
        prog: TransferProgress,
        update_cb: Callable[[], None],
    ) -> bool:
        same_drive = _same_drive(src, dst)

        if mode == "move" and same_drive:
            try:
                _clear_readonly(dst)
                os.replace(str(src), str(dst))
                prog.bytes_transferred += file_size
                update_cb()
                return True
            except OSError:
                pass

        dst_dir = dst.parent
        dst_dir.mkdir(parents=True, exist_ok=True)
        temp_dst = dst_dir / f".tmp_{os.getpid()}_{uuid.uuid4().hex[:8]}_{dst.name}"

        bytes_written_this_file = 0

        try:
            with open(src, "rb") as fsrc, open(temp_dst, "wb") as fdst:
                while True:
                    if self.is_cancelled():
                        break
                    chunk = fsrc.read(self._CHUNK_SIZE)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    chunk_len = len(chunk)
                    bytes_written_this_file += chunk_len
                    prog.bytes_transferred += chunk_len
                    update_cb()

            if self.is_cancelled():
                _safe_unlink(temp_dst)
                prog.bytes_transferred -= bytes_written_this_file
                return False

            if preserve_timestamps:
                try:
                    shutil.copystat(str(src), str(temp_dst))
                except OSError:
                    pass

            _clear_readonly(dst)
            os.replace(str(temp_dst), str(dst))

            if mode == "move":
                try:
                    _clear_readonly(src)
                    os.unlink(str(src))
                except OSError as e:
                    self._logger.warn(f"Copied {src} but failed to delete original: {e}")

            return True

        except Exception:
            _safe_unlink(temp_dst)
            prog.bytes_transferred -= bytes_written_this_file
            raise

    # ── Conflict Resolution Handler ──────────────────────────────────────────

    def _resolve_conflict(self, src: Path, dst: Path, policy: str, mode: str) -> str:
        if self.is_cancelled():
            return "cancel"

        if policy == "skip":
            return "skip"
        if policy == "replace":
            return "replace"
        if policy == "rename":
            return "rename"

        if self._apply_all_conflict:
            return self._apply_all_conflict

        if not self._root:
            return "skip"

        result = {"action": "skip", "apply_all": False}
        evt = threading.Event()

        def _show():
            try:
                if not self._root.winfo_exists():
                    result["action"] = "cancel"
                    return
                dlg = ConflictDialog(self._root, src, dst, mode)
                dlg.wait_window()
                result["action"] = dlg.selected_action
                result["apply_all"] = dlg.apply_to_all
            except Exception as e:
                self._logger.warn(f"Conflict dialog error: {e}")
                result["action"] = "skip"
            finally:
                evt.set()

        self._root.after(0, _show)
        evt.wait()

        action = result.get("action", "skip")
        if result.get("apply_all") and action in ("replace", "skip", "rename"):
            self._apply_all_conflict = action
            self._logger.info(f"Duplicate preference set to '{action.upper()}' for all remaining duplicates.")

        return action

    # ── Error Resolution Handler ─────────────────────────────────────────────

    def _resolve_error(self, file_path: Path, error: Exception, mode: str) -> str:
        if self.is_cancelled():
            return "abort"

        if self._apply_all_error:
            return self._apply_all_error

        if not self._root:
            return "skip"

        result = {"action": "skip", "apply_all": False}
        evt = threading.Event()

        def _show():
            try:
                if not self._root.winfo_exists():
                    result["action"] = "abort"
                    return
                dlg = ErrorDialog(self._root, file_path, error, mode)
                dlg.wait_window()
                result["action"] = dlg.selected_action
                result["apply_all"] = dlg.apply_to_all
            except Exception as e:
                self._logger.warn(f"Error dialog exception: {e}")
                result["action"] = "skip"
            finally:
                evt.set()

        self._root.after(0, _show)
        evt.wait()

        action = result.get("action", "skip")
        if result.get("apply_all") and action == "skip":
            self._apply_all_error = "skip"
            self._logger.info("Automatically skipping all future transfer errors without prompting.")

        return action

    @staticmethod
    def _generate_unique_name(target: Path) -> Path:
        stem = target.stem
        suffix = target.suffix
        parent = target.parent
        counter = 1
        while True:
            candidate = parent / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1


# Backwards compatibility alias
MoveEngine = TransferEngine


# ─────────────────────────────────────────────
# Progress Manager
# ─────────────────────────────────────────────

class ProgressManager:
    def __init__(self, q: queue.Queue):
        self._q = q

    def push(self, prog: TransferProgress) -> None:
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
        self.sources: list[Path] = []
        self.dest: Optional[Path] = None
        self.scan_result: Optional[ScanResult] = None
        self.mode: str = "move"              # "move" or "copy"
        self.conflict_policy: str = "ask"    # "ask", "skip", "replace", "rename"
        self.skip_node_modules: bool = True
        self.preserve_timestamps: bool = True


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
    "cyan":     "#06b6d4",
    "cyan_d":   "#0891b2",
    "red":      "#ef4444",
    "red_d":    "#b91c1c",
    "amber":    "#f59e0b",
    "amber_d":  "#d97706",
    "purple":   "#8b5cf6",
    "muted":    "#4b5563",
    "subtle":   "#374151",
    "dim":      "#6b7280",
    "text":     "#e2e8f0",
    "text_dim": "#94a3b8",
    "tag_folder": "#1e3a5f",
    "tag_file":   "#2d1b4e",
}


# ─────────────────────────────────────────────
# Conflict Dialog (Duplicate File Found)
# ─────────────────────────────────────────────

class ConflictDialog(ctk.CTkToplevel):
    """
    Modal dialog when a duplicate file exists in the destination.
    Offers Replace, Skip, Auto-Rename, and Cancel with 'Apply to all' checkbox.
    """

    def __init__(self, parent: ctk.CTk, src: Path, dst: Path, mode: str):
        super().__init__(parent)
        self.selected_action = "skip"
        self.apply_to_all = False

        self.title("Duplicate File Detected")
        self.geometry("640x480")
        self.resizable(False, False)
        self.configure(fg_color=_C["bg"])

        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + (parent.winfo_width() - 640) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - 480) // 2
            self.geometry(f"+{max(20, px)}+{max(20, py)}")
        except Exception:
            pass

        self._build_ui(src, dst, mode)

    def _build_ui(self, src: Path, dst: Path, mode: str) -> None:
        self.grid_columnconfigure(0, weight=1)

        top_f = ctk.CTkFrame(self, fg_color=_C["surface"], corner_radius=10)
        top_f.grid(row=0, column=0, padx=16, pady=(16, 10), sticky="ew")
        top_f.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top_f, text="⚠️", font=ctk.CTkFont(size=28)).grid(row=0, column=0, padx=(14, 10), pady=12)

        info_box = ctk.CTkFrame(top_f, fg_color="transparent")
        info_box.grid(row=0, column=1, sticky="w", pady=10)
        ctk.CTkLabel(
            info_box, text="Duplicate File Found",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=_C["amber"],
        ).pack(anchor="w")

        action_desc = "move" if mode == "move" else "copy"
        ctk.CTkLabel(
            info_box,
            text=f"A file with this name already exists in destination. How would you like to handle this?",
            font=ctk.CTkFont(size=12),
            text_color=_C["text_dim"],
        ).pack(anchor="w")

        name_box = ctk.CTkFrame(self, fg_color=_C["input"], corner_radius=6)
        name_box.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
        ctk.CTkLabel(
            name_box, text=f"📄  {src.name}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=_C["text"],
        ).pack(padx=12, pady=8, anchor="w")

        cards_f = ctk.CTkFrame(self, fg_color="transparent")
        cards_f.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        cards_f.grid_columnconfigure((0, 1), weight=1)

        src_info = self._get_file_info(src)
        dst_info = self._get_file_info(dst)

        # Source Card
        src_card = ctk.CTkFrame(cards_f, fg_color=_C["surface"], corner_radius=8)
        src_card.grid(row=0, column=0, padx=(0, 6), sticky="nsew")
        ctk.CTkLabel(
            src_card, text=f"Incoming Source ({mode.upper()})",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=_C["green"] if mode == "move" else _C["cyan"],
        ).pack(padx=10, pady=(8, 4), anchor="w")

        ctk.CTkLabel(src_card, text=f"Size: {src_info['size']}", font=ctk.CTkFont(size=11), text_color=_C["text"]).pack(padx=10, pady=1, anchor="w")
        ctk.CTkLabel(src_card, text=f"Modified: {src_info['mtime']}", font=ctk.CTkFont(size=11), text_color=_C["text_dim"]).pack(padx=10, pady=1, anchor="w")
        ctk.CTkLabel(src_card, text=f"Folder: {src.parent.name}", font=ctk.CTkFont(size=10), text_color=_C["dim"]).pack(padx=10, pady=(1, 8), anchor="w")

        # Destination Card
        dst_card = ctk.CTkFrame(cards_f, fg_color=_C["surface"], corner_radius=8)
        dst_card.grid(row=0, column=1, padx=(6, 0), sticky="nsew")
        ctk.CTkLabel(
            dst_card, text="Existing Destination",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=_C["amber"],
        ).pack(padx=10, pady=(8, 4), anchor="w")

        ctk.CTkLabel(dst_card, text=f"Size: {dst_info['size']}", font=ctk.CTkFont(size=11), text_color=_C["text"]).pack(padx=10, pady=1, anchor="w")
        ctk.CTkLabel(dst_card, text=f"Modified: {dst_info['mtime']}", font=ctk.CTkFont(size=11), text_color=_C["text_dim"]).pack(padx=10, pady=1, anchor="w")
        ctk.CTkLabel(dst_card, text=f"Folder: {dst.parent.name}", font=ctk.CTkFont(size=10), text_color=_C["dim"]).pack(padx=10, pady=(1, 8), anchor="w")

        note = self._get_comparison_note(src_info, dst_info)
        if note:
            ctk.CTkLabel(
                self, text=note,
                font=ctk.CTkFont(size=11, slant="italic"),
                text_color=_C["text_dim"],
            ).grid(row=3, column=0, padx=20, pady=4, sticky="w")

        self._apply_all_var = ctk.BooleanVar(value=False)
        chk = ctk.CTkCheckBox(
            self,
            text="Apply this decision to all remaining duplicate conflicts",
            variable=self._apply_all_var,
            font=ctk.CTkFont(size=12),
            text_color=_C["text"],
            fg_color=_C["blue"],
            hover_color=_C["blue_d"],
            border_color=_C["border"],
        )
        chk.grid(row=4, column=0, padx=20, pady=(10, 4), sticky="w")

        hint_txt = "💡 Replace: Overwrite destination (transfers file)  ·  Skip: Leave source & destination untouched  ·  Rename: Keep both"
        ctk.CTkLabel(
            self, text=hint_txt,
            font=ctk.CTkFont(size=10),
            text_color=_C["dim"],
        ).grid(row=5, column=0, padx=20, pady=(0, 12), sticky="w")

        btn_f = ctk.CTkFrame(self, fg_color="transparent")
        btn_f.grid(row=6, column=0, padx=16, pady=(0, 16), sticky="ew")
        btn_f.grid_columnconfigure((0, 1, 2, 3), weight=1)

        ctk.CTkButton(
            btn_f, text="🔄  Replace",
            command=lambda: self._select("replace"),
            fg_color=_C["amber"], hover_color=_C["amber_d"],
            text_color="#000000",
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34, corner_radius=7,
        ).grid(row=0, column=0, padx=4, sticky="ew")

        ctk.CTkButton(
            btn_f, text="⏭️  Skip",
            command=lambda: self._select("skip"),
            fg_color=_C["subtle"], hover_color=_C["muted"],
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34, corner_radius=7,
        ).grid(row=0, column=1, padx=4, sticky="ew")

        ctk.CTkButton(
            btn_f, text="📝  Auto-Rename",
            command=lambda: self._select("rename"),
            fg_color=_C["blue"], hover_color=_C["blue_d"],
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34, corner_radius=7,
        ).grid(row=0, column=2, padx=4, sticky="ew")

        ctk.CTkButton(
            btn_f, text="✖  Cancel",
            command=lambda: self._select("cancel"),
            fg_color=_C["raised"], hover_color=_C["red_d"],
            font=ctk.CTkFont(size=12),
            height=34, corner_radius=7,
        ).grid(row=0, column=3, padx=4, sticky="ew")

    def _select(self, action: str) -> None:
        self.selected_action = action
        self.apply_to_all = self._apply_all_var.get()
        self.destroy()

    def _on_close(self) -> None:
        self.selected_action = "skip"
        self.apply_to_all = False
        self.destroy()

    @staticmethod
    def _get_file_info(p: Path) -> dict:
        try:
            st = p.stat()
            return {
                "size_raw": st.st_size,
                "size": _fmt_size(st.st_size),
                "mtime_raw": st.st_mtime,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        except OSError:
            return {"size_raw": 0, "size": "Unknown", "mtime_raw": 0, "mtime": "Unknown"}

    @staticmethod
    def _get_comparison_note(src_info: dict, dst_info: dict) -> str:
        notes = []
        if src_info["size_raw"] == dst_info["size_raw"] and src_info["size_raw"] > 0:
            notes.append("Files have identical size.")
        if src_info["mtime_raw"] > dst_info["mtime_raw"]:
            notes.append("Source file is newer.")
        elif dst_info["mtime_raw"] > src_info["mtime_raw"]:
            notes.append("Destination file is newer.")
        return "  ·  ".join(notes)


# ─────────────────────────────────────────────
# Error Dialog
# ─────────────────────────────────────────────

class ErrorDialog(ctk.CTkToplevel):
    """
    Modal dialog when an error occurs during file transfer.
    Offers Retry, Skip, Abort, with 'Apply to all future errors' checkbox.
    """

    def __init__(self, parent: ctk.CTk, file_path: Path, error: Exception, mode: str):
        super().__init__(parent)
        self.selected_action = "skip"
        self.apply_to_all = False

        self.title("Transfer Error")
        self.geometry("580x360")
        self.resizable(False, False)
        self.configure(fg_color=_C["bg"])

        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + (parent.winfo_width() - 580) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - 360) // 2
            self.geometry(f"+{max(20, px)}+{max(20, py)}")
        except Exception:
            pass

        self._build_ui(file_path, error, mode)

    def _build_ui(self, file_path: Path, error: Exception, mode: str) -> None:
        self.grid_columnconfigure(0, weight=1)

        top_f = ctk.CTkFrame(self, fg_color=_C["surface"], corner_radius=10)
        top_f.grid(row=0, column=0, padx=16, pady=(16, 10), sticky="ew")
        top_f.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top_f, text="❌", font=ctk.CTkFont(size=26)).grid(row=0, column=0, padx=(14, 10), pady=12)

        info_box = ctk.CTkFrame(top_f, fg_color="transparent")
        info_box.grid(row=0, column=1, sticky="w", pady=10)
        ctk.CTkLabel(
            info_box, text=f"File {mode.capitalize()} Error",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=_C["red"],
        ).pack(anchor="w")

        ctk.CTkLabel(
            info_box,
            text=f"An error occurred while attempting to {mode} this file.",
            font=ctk.CTkFont(size=11),
            text_color=_C["text_dim"],
        ).pack(anchor="w")

        detail_box = ctk.CTkFrame(self, fg_color=_C["raised"], corner_radius=8)
        detail_box.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
        detail_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            detail_box, text=f"File: {file_path.name}",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=_C["text"], anchor="w",
        ).pack(padx=12, pady=(10, 2), anchor="w")

        err_text = str(error) or type(error).__name__
        ctk.CTkLabel(
            detail_box, text=f"Error: {err_text}",
            font=ctk.CTkFont(size=11),
            text_color=_C["amber"], anchor="w", wraplength=520,
        ).pack(padx=12, pady=(2, 6), anchor="w")

        tip = "Tip: If the file is open in another program, close it and click Retry."
        ctk.CTkLabel(
            detail_box, text=tip,
            font=ctk.CTkFont(size=11, slant="italic"),
            text_color=_C["dim"], anchor="w",
        ).pack(padx=12, pady=(0, 10), anchor="w")

        self._apply_all_var = ctk.BooleanVar(value=False)
        chk = ctk.CTkCheckBox(
            self,
            text="Apply to all future errors (Automatically skip without prompting)",
            variable=self._apply_all_var,
            font=ctk.CTkFont(size=12),
            text_color=_C["text"],
            fg_color=_C["blue"],
            hover_color=_C["blue_d"],
            border_color=_C["border"],
        )
        chk.grid(row=2, column=0, padx=20, pady=(12, 14), sticky="w")

        btn_f = ctk.CTkFrame(self, fg_color="transparent")
        btn_f.grid(row=3, column=0, padx=16, pady=(0, 16), sticky="ew")
        btn_f.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkButton(
            btn_f, text="🔄  Retry",
            command=lambda: self._select("retry"),
            fg_color=_C["green"], hover_color=_C["green_d"],
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34, corner_radius=7,
        ).grid(row=0, column=0, padx=6, sticky="ew")

        ctk.CTkButton(
            btn_f, text="⏭️  Skip File",
            command=lambda: self._select("skip"),
            fg_color=_C["subtle"], hover_color=_C["muted"],
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34, corner_radius=7,
        ).grid(row=0, column=1, padx=6, sticky="ew")

        ctk.CTkButton(
            btn_f, text="✖  Abort Transfer",
            command=lambda: self._select("abort"),
            fg_color=_C["raised"], hover_color=_C["red_d"],
            font=ctk.CTkFont(size=12),
            height=34, corner_radius=7,
        ).grid(row=0, column=2, padx=6, sticky="ew")

    def _select(self, action: str) -> None:
        self.selected_action = action
        self.apply_to_all = self._apply_all_var.get()
        self.destroy()

    def _on_close(self) -> None:
        self.selected_action = "skip"
        self.apply_to_all = False
        self.destroy()


# ─────────────────────────────────────────────
# Source list widget with Drop Zone
# ─────────────────────────────────────────────

class SourceList(ctk.CTkScrollableFrame):
    """Scrollable list showing added source folders/files with drag-and-drop placeholder."""

    ICON_FOLDER = "📁"
    ICON_FILE   = "📄"

    def __init__(self, master, on_change: Callable[[], None], **kwargs):
        super().__init__(master, **kwargs)
        self._on_change = on_change
        self._paths: list[Path] = []
        self._empty_frame: Optional[ctk.CTkFrame] = None
        self.grid_columnconfigure(0, weight=1)
        self._show_empty_placeholder()

    def _show_empty_placeholder(self) -> None:
        if not self._paths:
            self._empty_frame = ctk.CTkFrame(self, fg_color="transparent")
            self._empty_frame.grid(row=0, column=0, pady=18, sticky="ew")
            self._empty_frame.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                self._empty_frame,
                text="📥  Drag & Drop folders and files anywhere here",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=_C["text_dim"],
            ).grid(row=0, column=0)

            ctk.CTkLabel(
                self._empty_frame,
                text="or click 'Add Folder' / 'Add Files' (or Ctrl+V to paste)",
                font=ctk.CTkFont(size=11),
                text_color=_C["dim"],
            ).grid(row=1, column=0, pady=(2, 0))

    def _hide_empty_placeholder(self) -> None:
        if self._empty_frame:
            self._empty_frame.destroy()
            self._empty_frame = None

    def add(self, path: Path) -> bool:
        """Add a path. Returns False if already present."""
        if path in self._paths:
            return False

        self._hide_empty_placeholder()
        self._paths.append(path)
        self._render_row(path, len(self._paths) - 1)
        self._on_change()
        return True

    def _render_row(self, path: Path, row_idx: int) -> None:
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

        ctk.CTkButton(
            frame, text="✕", width=26, height=26,
            fg_color=_C["subtle"], hover_color=_C["red_d"],
            font=ctk.CTkFont(size=11), corner_radius=4,
            command=lambda p=path: self.remove(p),
        ).grid(row=0, column=2, padx=(0, 8), pady=6)

    def remove(self, path: Path) -> None:
        if path in self._paths:
            self._paths.remove(path)
            self._rebuild()
            self._on_change()

    def _rebuild(self) -> None:
        for w in self.winfo_children():
            w.destroy()
        self._empty_frame = None

        if not self._paths:
            self._show_empty_placeholder()
        else:
            for idx, p in enumerate(self._paths):
                self._render_row(p, idx)

    def paths(self) -> list[Path]:
        return list(self._paths)

    def clear(self) -> None:
        self._paths.clear()
        for w in self.winfo_children():
            w.destroy()
        self._empty_frame = None
        self._show_empty_placeholder()
        self._on_change()

    def count(self) -> int:
        return len(self._paths)


# ─────────────────────────────────────────────
# Main Application
# ─────────────────────────────────────────────

class App(_BaseApp):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("⚡ Fast File Mover & Copier")
        self.geometry("1020x950")
        self.minsize(860, 760)
        self.configure(fg_color=_C["bg"])

        self._settings = Settings()
        self._logger = Logger(Path.home() / "fast_file_mover_errors.log")
        self._logger.add_handler(self._append_log)

        self._scanner = Scanner(self._logger)
        self._engine: Optional[TransferEngine] = None
        self._prog_queue: queue.Queue = queue.Queue(maxsize=60)
        self._prog_manager = ProgressManager(self._prog_queue)

        self._scan_thread: Optional[threading.Thread] = None
        self._move_thread: Optional[threading.Thread] = None
        self._running_transfer = False
        self._stat_labels: dict[str, ctk.CTkLabel] = {}

        self._build_ui()
        self._poll_progress()

        # Initialize Drag & Drop if TkinterDnD is available
        if _HAS_DND:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._on_dnd_drop)
            except Exception as e:
                self._logger.warn(f"Drag & Drop init warning: {e}")

        # Bind Clipboard Paste (Ctrl+V) as a handy fallback
        self.bind("<Control-v>", lambda e: self._paste_from_clipboard())
        self.bind("<Control-V>", lambda e: self._paste_from_clipboard())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Drag & Drop Event Handler (TkinterDnD) ───────────────────────────────

    def _on_dnd_drop(self, event) -> None:
        """Handles dropped files or folders from Windows Explorer cleanly."""
        raw_data = getattr(event, "data", "")
        if not raw_data:
            return

        try:
            items = self.tk.splitlist(raw_data)
        except Exception:
            items = [raw_data]

        # Check if dropped onto Destination section
        is_dst = False
        if hasattr(self, "_dst_frame"):
            try:
                target_w = self.winfo_containing(event.x_root, event.y_root)
                curr = target_w
                while curr is not None:
                    if curr == self._dst_frame:
                        is_dst = True
                        break
                    curr = getattr(curr, "master", None)
            except Exception:
                pass

        if is_dst and items:
            p = Path(items[0].strip('"').strip("'"))
            if p.is_dir():
                self._settings.dest = p
                self._dst_var.set(str(p))
                self._settings.scan_result = None
                self._transfer_btn.configure(state="disabled")
                free_sp = _get_free_space(p)
                self._set_stat("dest_free", _fmt_size(free_sp))
                self._set_status(f"Destination set via Drag & Drop: {p.name}")
                self._logger.info(f"Destination set via Drag & Drop: {p}")
                return
            else:
                self._set_status("Dropped item on Destination must be a folder.")

        # Otherwise add all items to source
        added_files = 0
        added_folders = 0
        for item in items:
            clean_path = item.strip('"').strip("'")
            p = Path(clean_path)
            if p.exists():
                if self._source_list.add(p):
                    if p.is_dir():
                        added_folders += 1
                    else:
                        added_files += 1

        total_added = added_files + added_folders
        if total_added > 0:
            parts = []
            if added_folders:
                parts.append(f"{added_folders} folder{'s' if added_folders != 1 else ''}")
            if added_files:
                parts.append(f"{added_files} file{'s' if added_files != 1 else ''}")
            msg = f"Added {' and '.join(parts)} via Drag & Drop."
            self._set_status(msg)
            self._logger.info(msg)

    def _paste_from_clipboard(self) -> None:
        """Allows pasting files or paths copied into Windows Clipboard (Ctrl+V)."""
        try:
            cb = self.clipboard_get()
            if not cb:
                return
            lines = cb.splitlines()
            added = 0
            for line in lines:
                p = Path(line.strip().strip('"').strip("'"))
                if p.exists() and self._source_list.add(p):
                    added += 1
            if added:
                self._set_status(f"Pasted {added} item(s) from clipboard.")
                self._logger.info(f"Pasted {added} item(s) from clipboard.")
        except Exception:
            pass

    def _on_close(self) -> None:
        if self._running_transfer:
            if not messagebox.askyesno("Exit Confirmation", "A transfer is currently in progress. Cancel transfer and exit?"):
                return
            if self._engine:
                self._engine.cancel()
        self.destroy()

    # ── UI Construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
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
        ctk.CTkLabel(title_f, text="Fast File Mover & Copier", font=ctk.CTkFont(size=20, weight="bold"), text_color=_C["text"]).grid(row=0, column=1)

        dnd_text = "Drag & Drop supported" if _HAS_DND else "Multi-source transfer"
        self._hdr_subtitle = ctk.CTkLabel(
            hdr, text=f"{dnd_text}  ·  Move & Copy modes  ·  Smart duplicate & error handling",
            font=ctk.CTkFont(size=12), text_color=_C["dim"],
        )
        self._hdr_subtitle.grid(row=0, column=1, padx=8, pady=14, sticky="w")

        self._mode_badge = ctk.CTkLabel(
            hdr, text="MOVE MODE",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=_C["green"],
            fg_color=_C["raised"],
            corner_radius=6,
            padx=10, pady=4,
        )
        self._mode_badge.grid(row=0, column=2, padx=16, pady=14, sticky="e")

    # ── Pickers & Settings ───────────────────────────────────────────────────

    def _build_pickers(self) -> None:
        outer = ctk.CTkFrame(self._scroll, fg_color=_C["surface"], corner_radius=12)
        outer.grid(row=1, column=0, padx=12, pady=4, sticky="ew")
        outer.grid_columnconfigure(0, weight=1)

        # ── SOURCE section ──
        src_header = ctk.CTkFrame(outer, fg_color="transparent")
        src_header.grid(row=0, column=0, padx=14, pady=(12, 4), sticky="ew")
        src_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            src_header, text="SOURCE  (Drag & drop folders or files here)",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=_C["dim"],
        ).grid(row=0, column=0, sticky="w")

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
            btn_row, text="📋  Paste",
            command=self._paste_from_clipboard,
            fg_color=_C["subtle"], hover_color=_C["muted"],
            font=ctk.CTkFont(size=12), width=100, height=32, corner_radius=7,
        ).grid(row=0, column=2, padx=(0, 8))

        ctk.CTkButton(
            btn_row, text="🗑  Clear All",
            command=self._clear_sources,
            fg_color=_C["raised"], hover_color=_C["red_d"],
            font=ctk.CTkFont(size=12), width=110, height=32, corner_radius=7,
        ).grid(row=0, column=3)

        self._source_list = SourceList(
            outer,
            on_change=self._on_sources_changed,
            fg_color=_C["input"],
            corner_radius=8,
            height=100,
            scrollbar_button_color=_C["border"],
            scrollbar_button_hover_color=_C["subtle"],
        )
        self._source_list.grid(row=2, column=0, padx=14, pady=(0, 8), sticky="ew")

        self._src_count_lbl = ctk.CTkLabel(
            outer, text="No sources added",
            font=ctk.CTkFont(size=11), text_color=_C["dim"], anchor="w",
        )
        self._src_count_lbl.grid(row=3, column=0, padx=16, pady=(0, 4), sticky="w")

        # ── Separator ──
        ctk.CTkFrame(outer, height=1, fg_color=_C["border"]).grid(
            row=4, column=0, padx=14, pady=4, sticky="ew"
        )

        # ── DESTINATION row ──
        dst_header = ctk.CTkFrame(outer, fg_color="transparent")
        dst_header.grid(row=5, column=0, padx=14, pady=(6, 4), sticky="ew")
        ctk.CTkLabel(
            dst_header, text="DESTINATION  (Drag & drop destination folder here)",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=_C["dim"],
        ).grid(row=0, column=0, sticky="w")

        self._dst_frame = ctk.CTkFrame(outer, fg_color="transparent")
        self._dst_frame.grid(row=6, column=0, padx=14, pady=(0, 6), sticky="ew")
        self._dst_frame.grid_columnconfigure(0, weight=1)

        self._dst_var = ctk.StringVar()
        ctk.CTkEntry(
            self._dst_frame, textvariable=self._dst_var,
            state="readonly",
            placeholder_text="Select destination or drag & drop folder here...",
            font=ctk.CTkFont(size=12),
            fg_color=_C["input"], border_color=_C["border"],
            text_color=_C["text"], height=34,
        ).grid(row=0, column=0, padx=(0, 8), sticky="ew")

        ctk.CTkButton(
            self._dst_frame, text="📁  Browse",
            command=self._pick_dest,
            fg_color=_C["subtle"], hover_color=_C["muted"],
            font=ctk.CTkFont(size=12), width=110, height=34, corner_radius=7,
        ).grid(row=0, column=1)

        # ── Separator ──
        ctk.CTkFrame(outer, height=1, fg_color=_C["border"]).grid(
            row=7, column=0, padx=14, pady=4, sticky="ew"
        )

        # ── TRANSFER OPTIONS ──
        opt_hdr = ctk.CTkFrame(outer, fg_color="transparent")
        opt_hdr.grid(row=8, column=0, padx=14, pady=(6, 4), sticky="ew")
        ctk.CTkLabel(
            opt_hdr, text="TRANSFER OPTIONS",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=_C["dim"],
        ).grid(row=0, column=0, sticky="w")

        opts_controls = ctk.CTkFrame(outer, fg_color="transparent")
        opts_controls.grid(row=9, column=0, padx=14, pady=(0, 6), sticky="ew")
        opts_controls.grid_columnconfigure((0, 1), weight=1)

        # Mode Box
        mode_box = ctk.CTkFrame(opts_controls, fg_color=_C["raised"], corner_radius=8)
        mode_box.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkLabel(mode_box, text="Operation Mode:", font=ctk.CTkFont(size=11, weight="bold"), text_color=_C["text_dim"]).pack(padx=10, pady=(6, 2), anchor="w")
        self._mode_seg = ctk.CTkSegmentedButton(
            mode_box,
            values=["Move Files", "Copy Files"],
            command=self._on_mode_change,
            selected_color=_C["green"],
            selected_hover_color=_C["green_d"],
            unselected_color=_C["input"],
            font=ctk.CTkFont(size=12, weight="bold"),
            height=30,
        )
        self._mode_seg.set("Move Files")
        self._mode_seg.pack(padx=10, pady=(0, 8), fill="x")

        # Duplicate Policy Box
        dup_box = ctk.CTkFrame(opts_controls, fg_color=_C["raised"], corner_radius=8)
        dup_box.grid(row=0, column=1, padx=(6, 0), sticky="ew")
        ctk.CTkLabel(dup_box, text="Duplicate Handling:", font=ctk.CTkFont(size=11, weight="bold"), text_color=_C["text_dim"]).pack(padx=10, pady=(6, 2), anchor="w")
        self._conflict_var = ctk.StringVar(value="Ask on duplicate")
        self._conflict_menu = ctk.CTkOptionMenu(
            dup_box,
            variable=self._conflict_var,
            values=[
                "Ask on duplicate",
                "Skip duplicates",
                "Replace duplicates",
                "Auto-rename (Keep both)",
            ],
            command=self._on_conflict_policy_change,
            fg_color=_C["input"],
            button_color=_C["subtle"],
            button_hover_color=_C["muted"],
            font=ctk.CTkFont(size=12),
            height=30,
        )
        self._conflict_menu.pack(padx=10, pady=(0, 8), fill="x")

        # Checkboxes row
        opts_checks = ctk.CTkFrame(outer, fg_color="transparent")
        opts_checks.grid(row=10, column=0, padx=14, pady=(4, 10), sticky="ew")

        self._skip_nm_var = ctk.BooleanVar(value=True)
        self._skip_nm_cb = ctk.CTkCheckBox(
            opts_checks,
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
            opts_checks,
            text="● ACTIVE",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=_C["green"],
        )
        self._skip_nm_badge.grid(row=0, column=1, padx=(10, 24), sticky="w")

        self._preserve_ts_var = ctk.BooleanVar(value=True)
        self._preserve_ts_cb = ctk.CTkCheckBox(
            opts_checks,
            text="Preserve file timestamps & metadata",
            variable=self._preserve_ts_var,
            onvalue=True, offvalue=False,
            font=ctk.CTkFont(size=12),
            text_color=_C["text"],
            fg_color=_C["blue"],
            hover_color=_C["blue_d"],
            checkmark_color="#ffffff",
            border_color=_C["border"],
        )
        self._preserve_ts_cb.grid(row=0, column=2, sticky="w")

        # ── Action buttons row ──
        ctk.CTkFrame(outer, height=1, fg_color=_C["border"]).grid(
            row=11, column=0, padx=14, pady=(0, 8), sticky="ew"
        )
        action_row = ctk.CTkFrame(outer, fg_color="transparent")
        action_row.grid(row=12, column=0, padx=14, pady=(0, 14), sticky="w")

        self._scan_btn = ctk.CTkButton(
            action_row, text="🔍  Scan",
            command=self._start_scan,
            fg_color=_C["blue"], hover_color=_C["blue_d"],
            font=ctk.CTkFont(size=13, weight="bold"),
            width=120, height=36, corner_radius=8,
        )
        self._scan_btn.grid(row=0, column=0, padx=(0, 8))

        self._transfer_btn = ctk.CTkButton(
            action_row, text="🚀  Move Files",
            command=self._start_transfer,
            fg_color=_C["green"], hover_color=_C["green_d"],
            font=ctk.CTkFont(size=13, weight="bold"),
            state="disabled", width=150, height=36, corner_radius=8,
        )
        self._transfer_btn.grid(row=0, column=1, padx=(0, 8))

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

        # Card 1: Scan Results
        scan_card = self._make_card(cards_frame, "📊  Scan Results", 0)
        for i, (key, lbl) in enumerate([
            ("files",          "Files"),
            ("folders",        "Folders"),
            ("size",           "Total Size"),
            ("largest_file",   "Largest File"),
            ("largest_folder", "Largest Folder"),
            ("avg_size",       "Avg File Size"),
            ("skipped_nm",     "Skipped node_modules"),
            ("dest_free",      "Dest Free Space"),
        ]):
            self._make_stat_row(scan_card, i, lbl, key)

        # Card 2: Transfer Settings & Estimate
        est_card = self._make_card(cards_frame, "⚙️  Transfer Config & ETA", 1)
        for i, (key, lbl) in enumerate([
            ("cfg_mode",       "Active Mode"),
            ("cfg_conflict",   "Duplicate Action"),
            ("est_speed",      "Est. Speed"),
            ("est_time",       "Est. Time Remaining"),
        ]):
            self._make_stat_row(est_card, i, lbl, key)

        # Card 3: Live Transfer
        live_card = self._make_card(cards_frame, "📡  Live Transfer", 2)
        for i, (key, lbl) in enumerate([
            ("cur_file",          "Current File"),
            ("files_transferred", "Files Transferred"),
            ("folders_affected",  "Folders Handled"),
            ("duplicates_skip",   "Duplicates Skipped"),
            ("renamed_files",     "Auto-Renamed"),
            ("errors",            "Errors"),
            ("speed",             "Current Speed"),
            ("avg_speed",         "Average Speed"),
            ("elapsed",           "Elapsed Time"),
            ("remaining",         "Remaining Time"),
        ]):
            self._make_stat_row(live_card, i, lbl, key)

        # Progress bar panel
        pb_outer = ctk.CTkFrame(outer, fg_color=_C["raised"], corner_radius=10)
        pb_outer.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")
        pb_outer.grid_columnconfigure(0, weight=1)

        pb_hdr = ctk.CTkFrame(pb_outer, fg_color="transparent")
        pb_hdr.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        pb_hdr.grid_columnconfigure(0, weight=1)

        self._pb_title = ctk.CTkLabel(
            pb_hdr, text="Transfer Progress",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=_C["text_dim"],
        )
        self._pb_title.grid(row=0, column=0, sticky="w")

        self._pct_label = ctk.CTkLabel(pb_hdr, text="0.0%", font=ctk.CTkFont(size=11, weight="bold"), text_color=_C["green"])
        self._pct_label.grid(row=0, column=1, sticky="e")

        self._progress = ctk.CTkProgressBar(pb_outer, height=16, corner_radius=8, progress_color=_C["green"], fg_color=_C["border"])
        self._progress.set(0)
        self._progress.grid(row=1, column=0, padx=14, pady=(0, 12), sticky="ew")

        self._set_stat("cfg_mode", "MOVE (Cut & Paste)")
        self._set_stat("cfg_conflict", "Ask on duplicate")

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
        ctk.CTkLabel(card, text=label + ":", font=ctk.CTkFont(size=11), text_color=_C["dim"], anchor="w", width=125).grid(
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

        hdr_log = ctk.CTkFrame(frame, fg_color="transparent")
        hdr_log.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        hdr_log.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(hdr_log, text="📋  Live Log", font=ctk.CTkFont(size=11, weight="bold"), text_color=_C["text_dim"]).grid(
            row=0, column=0, sticky="w"
        )

        ctk.CTkButton(
            hdr_log, text="Clear Log", width=70, height=22,
            fg_color=_C["raised"], hover_color=_C["subtle"],
            font=ctk.CTkFont(size=10),
            command=self._clear_log,
        ).grid(row=0, column=1, sticky="e")

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

        hint = "Ready — Drag & drop files/folders or press Ctrl+V to paste" if _HAS_DND else "Ready — Select sources or press Ctrl+V to paste"
        self._status_lbl = ctk.CTkLabel(frame, text=hint, font=ctk.CTkFont(size=12), text_color=_C["dim"], anchor="w")
        self._status_lbl.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            frame, text="Fast File Mover & Copier v2.0",
            font=ctk.CTkFont(size=11), text_color=_C["subtle"],
        ).grid(row=0, column=1, sticky="e")

    # ── Event Handlers ───────────────────────────────────────────────────────

    def _on_mode_change(self, choice: str) -> None:
        if choice == "Copy Files":
            self._settings.mode = "copy"
            self._mode_seg.configure(selected_color=_C["cyan"], selected_hover_color=_C["cyan_d"])
            self._transfer_btn.configure(
                text="📋  Copy Files",
                fg_color=_C["cyan"],
                hover_color=_C["cyan_d"],
            )
            self._progress.configure(progress_color=_C["cyan"])
            self._pct_label.configure(text_color=_C["cyan"])
            self._mode_badge.configure(text="COPY MODE", text_color=_C["cyan"])
            self._set_stat("cfg_mode", "COPY (Duplicate files)")
        else:
            self._settings.mode = "move"
            self._mode_seg.configure(selected_color=_C["green"], selected_hover_color=_C["green_d"])
            self._transfer_btn.configure(
                text="🚀  Move Files",
                fg_color=_C["green"],
                hover_color=_C["green_d"],
            )
            self._progress.configure(progress_color=_C["green"])
            self._pct_label.configure(text_color=_C["green"])
            self._mode_badge.configure(text="MOVE MODE", text_color=_C["green"])
            self._set_stat("cfg_mode", "MOVE (Cut & Paste)")

    def _on_conflict_policy_change(self, choice: str) -> None:
        if "Skip" in choice:
            self._settings.conflict_policy = "skip"
        elif "Replace" in choice:
            self._settings.conflict_policy = "replace"
        elif "Auto-rename" in choice:
            self._settings.conflict_policy = "rename"
        else:
            self._settings.conflict_policy = "ask"
        self._set_stat("cfg_conflict", choice)

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
        self._settings.scan_result = None
        self._transfer_btn.configure(state="disabled")

    def _on_skip_toggle(self) -> None:
        active = self._skip_nm_var.get()
        self._settings.skip_node_modules = active
        if active:
            self._skip_nm_badge.configure(text="● ACTIVE", text_color=_C["green"])
        else:
            self._skip_nm_badge.configure(text="○ INACTIVE", text_color=_C["amber"])
        self._settings.scan_result = None
        self._transfer_btn.configure(state="disabled")

    def _pick_dest(self) -> None:
        path = filedialog.askdirectory(title="Select Destination Folder")
        if path:
            self._settings.dest = Path(path)
            self._dst_var.set(path)
            self._settings.scan_result = None
            self._transfer_btn.configure(state="disabled")
            self._set_status("Destination set.")
            free_sp = _get_free_space(self._settings.dest)
            self._set_stat("dest_free", _fmt_size(free_sp))

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
                    messagebox.showerror("Error", f"Destination cannot be inside source folder:\n{src}")
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
        self._transfer_btn.configure(state="disabled")
        self._set_status("Scanning…")
        self._scanner = Scanner(self._logger)

        def _run() -> None:
            result = self._scanner.scan(
                self._settings.sources,
                dest=self._settings.dest,
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
        if r.dest_free_space:
            self._set_stat("dest_free", _fmt_size(r.dest_free_space))
        else:
            self._set_stat("dest_free", "—")

        self._scan_btn.configure(state="normal")
        self._transfer_btn.configure(state="normal")
        self._set_status(
            f"Scan complete — {r.total_files:,} files, {r.total_folders:,} folders, {_fmt_size(r.total_size)}"
        )

    # ── Transfer (Move / Copy) ───────────────────────────────────────────────

    def _start_transfer(self) -> None:
        if self._running_transfer:
            return
        if not self._validate():
            return
        if not self._settings.scan_result:
            messagebox.showerror("Error", "Please run Scan first.")
            return

        dst = self._settings.dest
        try:
            dst.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Error", f"Cannot create destination:\n{e}")
            return

        # Pre-flight check: Destination free disk space
        req_size = self._settings.scan_result.total_size
        free_space = _get_free_space(dst)
        if free_space > 0 and req_size > free_space:
            warn_msg = (
                f"⚠️ Disk Space Warning:\n\n"
                f"Selected files require: {_fmt_size(req_size)}\n"
                f"Destination free space: {_fmt_size(free_space)}\n\n"
                f"The destination drive does not appear to have enough free space.\n"
                f"Do you want to continue anyway?"
            )
            if not messagebox.askyesno("Disk Space Warning", warn_msg):
                return

        mode = self._settings.mode
        n = self._settings.scan_result.total_files
        action_word = "Move" if mode == "move" else "Copy"
        if not messagebox.askyesno(f"Confirm {action_word}", f"{action_word} {n:,} files to:\n{dst}\n\nProceed?"):
            return

        self._running_transfer = True
        self._engine = TransferEngine(self._logger, root_window=self)
        self._scan_btn.configure(state="disabled")
        self._transfer_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._progress.set(0)
        self._pct_label.configure(text="0.0%")
        self._set_status(f"{action_word}ing files…")

        # Reset live stats
        self._set_stat("cur_file", "—")
        self._set_stat("files_transferred", "0")
        self._set_stat("folders_affected", "0")
        self._set_stat("duplicates_skip", "0")
        self._set_stat("renamed_files", "0")
        self._set_stat("errors", "0")
        lbl = self._stat_labels.get("errors")
        if lbl:
            lbl.configure(text_color=_C["text"])
        self._set_stat("speed", "0.0 MB/s")
        self._set_stat("avg_speed", "0.0 MB/s")
        self._set_stat("elapsed", "0s")
        self._set_stat("remaining", "—")

        sources = list(self._settings.sources)
        skip_nm = self._settings.skip_node_modules
        conflict_policy = self._settings.conflict_policy
        preserve_ts = self._preserve_ts_var.get()

        def _run() -> None:
            result = None
            try:
                result = self._engine.run_transfer(
                    roots=sources,
                    dest=dst,
                    scan_result=self._settings.scan_result,
                    mode=mode,
                    conflict_policy=conflict_policy,
                    skip_node_modules=skip_nm,
                    preserve_timestamps=preserve_ts,
                    progress_cb=lambda p: self._prog_manager.push(p),
                )
            except Exception as e:
                self._logger.error(f"Transfer encountered unexpected error: {e}")
                p = TransferProgress()
                p.done = True
                p.errors += 1
                result = p
            finally:
                if result is not None:
                    self.after(0, self._on_transfer_done, result)

        self._move_thread = threading.Thread(target=_run, daemon=True)
        self._move_thread.start()

    def _on_transfer_done(self, prog: TransferProgress) -> None:
        self._running_transfer = False
        self._cancel_btn.configure(state="disabled")
        self._scan_btn.configure(state="normal")
        self._transfer_btn.configure(state="normal")

        if prog.cancelled:
            self._set_status("Transfer cancelled.")
            messagebox.showinfo("Cancelled", "File transfer was cancelled. Partial files were cleaned up.")
            return

        # Ensure completion stats are fully reflected on UI
        self._progress.set(1.0)
        self._pct_label.configure(text="100.0%")
        self._set_stat("remaining", "0s")
        self._set_stat("speed", "0.0 MB/s")
        self._set_stat("cur_file", "Finished")

        action_word = "moved" if prog.mode == "move" else "copied"
        msg = (
            f"✅  Transfer Complete!\n\n"
            f"Mode:                 {prog.mode.upper()}\n"
            f"Files {action_word}:          {prog.files_transferred:,}\n"
            f"Folders handled:      {prog.folders_affected:,}\n"
            f"Duplicates skipped:   {prog.skipped_duplicates:,}\n"
            f"Files auto-renamed:   {prog.renamed_files:,}\n"
            f"Skipped node_modules: {prog.skipped_node_modules}\n"
            f"Elapsed time:         {_fmt_time(prog.elapsed)}\n"
            f"Average speed:        {prog.avg_speed_mbps:.1f} MB/s\n"
            f"Errors:               {prog.errors}"
        )
        if prog.errors:
            msg += f"\n\nError log: {Path.home() / 'fast_file_mover_errors.log'}"

        if messagebox.askyesno("Complete", msg + "\n\nOpen destination folder?"):
            try:
                os.startfile(str(self._settings.dest))
            except Exception:
                pass

        self._set_status(f"Done — {prog.files_transferred:,} files {action_word} in {_fmt_time(prog.elapsed)}")

    # ── Cancel ───────────────────────────────────────────────────────────────

    def _cancel(self) -> None:
        if self._engine:
            self._engine.cancel()
        if self._scanner:
            self._scanner.cancel()
        self._cancel_btn.configure(state="disabled")
        self._set_status("Cancelling…")

    # ── Progress Polling ─────────────────────────────────────────────────────

    def _poll_progress(self) -> None:
        try:
            while True:
                prog: TransferProgress = self._prog_queue.get_nowait()
                self._apply_progress(prog)
        except queue.Empty:
            pass
        self.after(80, self._poll_progress)

    def _apply_progress(self, prog: TransferProgress) -> None:
        if prog.done and not prog.cancelled:
            self._progress.set(1.0)
            self._pct_label.configure(text="100.0%")
            self._set_stat("remaining", "0s")
            self._set_stat("speed", "0.0 MB/s")
        else:
            pct = min(prog.percent / 100.0, 1.0)
            self._progress.set(pct)
            self._pct_label.configure(text=f"{prog.percent:.1f}%")
            self._set_stat("speed", f"{prog.speed_mbps:.1f} MB/s")
            self._set_stat("remaining", _fmt_time(prog.remaining))

        self._set_stat("cur_file",          prog.current_file[:50] if prog.current_file else "—")
        self._set_stat("files_transferred", f"{prog.files_transferred:,}")
        self._set_stat("folders_affected",  f"{prog.folders_affected:,}")
        self._set_stat("duplicates_skip",   f"{prog.skipped_duplicates:,}")
        self._set_stat("renamed_files",     f"{prog.renamed_files:,}")
        self._set_stat("errors",            f"{prog.errors}")
        if prog.errors > 0:
            lbl = self._stat_labels.get("errors")
            if lbl:
                lbl.configure(text_color=_C["red"])

        self._set_stat("avg_speed",         f"{prog.avg_speed_mbps:.1f} MB/s")
        self._set_stat("elapsed",           _fmt_time(prog.elapsed))

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

    def _clear_log(self) -> None:
        self._log_box.delete("1.0", "end")


# ─────────────────────────────────────────────
# Utility Functions
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


def _get_free_space(path: Path) -> int:
    try:
        check = path
        while not check.exists() and check.parent != check:
            check = check.parent
        usage = shutil.disk_usage(str(check))
        return usage.free
    except Exception:
        return 0


def _same_drive(a: Path, b: Path) -> bool:
    try:
        return os.path.splitdrive(str(a))[0].lower() == os.path.splitdrive(str(b))[0].lower()
    except Exception:
        return False


def _clear_readonly(p: Path) -> None:
    try:
        if p.exists():
            os.chmod(str(p), stat.S_IWRITE)
    except Exception:
        pass


def _safe_unlink(p: Path) -> None:
    try:
        if p.exists():
            _clear_readonly(p)
            p.unlink(missing_ok=True)
    except Exception:
        pass


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
