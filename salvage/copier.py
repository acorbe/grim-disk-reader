"""Walks the selected subset of the source tree and drives the copy supervisor.

Traversal and copying are interleaved (depth-first, one directory's files at
a time) rather than building a full worklist up front: on a dying disk even
`readdir` can be slow or fail, so we want to start recovering files
immediately rather than stalling on a full-tree enumeration first.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from salvage import report
from salvage.selection import SelectionStore
from salvage.state_db import StateDB, to_rel
from salvage.worker import CopySupervisor


@dataclass
class FileEvent:
    path: str
    status: str  # 'success' | 'failed' | 'aborted' | 'already_done'
    bytes_copied: int
    size: Optional[int]
    error: Optional[str]


class CopyEngine:
    def __init__(
        self,
        source_root: Path,
        dest_root: Path,
        state: StateDB,
        selection: SelectionStore,
        supervisor: Optional[CopySupervisor] = None,
        manifest_root: Optional[Path] = None,
    ):
        self.source_root = Path(source_root)
        self.dest_root = Path(dest_root)
        self.state = state
        self.selection = selection
        self.supervisor = supervisor or CopySupervisor()
        # Mirror-tree of small YAML metadata stubs, one per file, instead of
        # the actual bytes. Optional: None disables it entirely.
        self.manifest_root = Path(manifest_root) if manifest_root is not None else None

    def run(
        self,
        stop_event: threading.Event,
        pause_event: threading.Event,
        on_file_start: Optional[Callable[[str], None]] = None,
        on_file_progress: Optional[Callable[[str, int], None]] = None,
        on_file_done: Optional[Callable[[FileEvent], None]] = None,
        on_dir_error: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.supervisor.start()
        try:
            self._walk_and_copy(
                stop_event, pause_event, on_file_start, on_file_progress, on_file_done, on_dir_error
            )
        finally:
            self._fix_directory_timestamps()

    def stop_and_shutdown(self) -> None:
        self.supervisor.shutdown()

    # ---- internals --------------------------------------------------------

    def _walk_and_copy(
        self,
        stop_event: threading.Event,
        pause_event: threading.Event,
        on_file_start,
        on_file_progress,
        on_file_done,
        on_dir_error,
    ) -> None:
        root = self.source_root

        def onerror(exc: OSError) -> None:
            bad_path = getattr(exc, "filename", None) or str(exc)
            try:
                rel = to_rel(os.path.relpath(bad_path, root))
            except ValueError:
                rel = bad_path
            self.state.upsert_pending(rel, "dir", None, None)
            self.state.mark_failed(rel, f"{type(exc).__name__}: {exc}")
            if on_dir_error:
                on_dir_error(rel, str(exc))

        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=onerror):
            if stop_event.is_set():
                return

            rel_dir = to_rel(os.path.relpath(dirpath, root))

            kept_dirnames = []
            for name in dirnames:
                rel_child = f"{rel_dir}/{name}" if rel_dir else name
                if self.selection.effective(rel_child) or self.selection.any_include_below(
                    rel_child
                ):
                    kept_dirnames.append(name)
            dirnames[:] = kept_dirnames

            if rel_dir:
                self._ensure_dest_dir(rel_dir, dirpath)

            filenames.sort()
            for name in filenames:
                if stop_event.is_set():
                    return
                _wait_while_paused(pause_event, stop_event)
                if stop_event.is_set():
                    return

                rel_file = f"{rel_dir}/{name}" if rel_dir else name
                if not self.selection.effective(rel_file):
                    continue
                self._copy_one(
                    rel_file,
                    os.path.join(dirpath, name),
                    stop_event,
                    pause_event,
                    on_file_start,
                    on_file_progress,
                    on_file_done,
                )

    def _write_manifest(
        self,
        rel_path: str,
        name: str,
        live_stat: Optional[os.stat_result] = None,
        kind: str = "file",
    ) -> None:
        if self.manifest_root is None:
            return
        try:
            data = report.file_record(
                rel_path, name, self.state, self.dest_root, live_stat=live_stat, kind=kind
            )
            report.write_manifest_stub(self.manifest_root, rel_path, data)
        except OSError:
            pass  # a manifest stub failing to write must never abort a copy pass

    def _ensure_dest_dir(self, rel_dir: str, src_abs_dir: str) -> None:
        dest_dir = self.dest_root / rel_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            st = os.stat(src_abs_dir)
            mtime = st.st_mtime
        except OSError:
            mtime = None
        rec = self.state.get_copy_record(rel_dir)
        if rec is None or rec.status != "success":
            self.state.upsert_pending(rel_dir, "dir", None, mtime)

    def _copy_one(
        self,
        rel_file: str,
        src_abs: str,
        stop_event: threading.Event,
        pause_event: threading.Event,
        on_file_start,
        on_file_progress,
        on_file_done,
    ) -> None:
        dest_abs = self.dest_root / rel_file
        name = rel_file.rsplit("/", 1)[-1]

        if _is_symlink(src_abs):
            dest_abs.parent.mkdir(parents=True, exist_ok=True)
            self.state.upsert_pending(rel_file, "file", None, None)
            self._copy_symlink(rel_file, name, src_abs, dest_abs, on_file_done)
            return

        try:
            st = os.stat(src_abs)
            size, mtime, atime = st.st_size, st.st_mtime, st.st_atime
        except OSError as exc:
            self.state.upsert_pending(rel_file, "file", None, None)
            self.state.mark_failed(rel_file, f"stat failed: {exc}")
            self._write_manifest(rel_file, name)
            if on_file_done:
                on_file_done(FileEvent(rel_file, "failed", 0, None, f"stat failed: {exc}"))
            return

        if not stat.S_ISREG(st.st_mode):
            self.state.upsert_pending(rel_file, "file", size, mtime)
            self.state.mark_skipped(rel_file, "not a regular file (device/socket/fifo)")
            self._write_manifest(rel_file, name, live_stat=st)
            if on_file_done:
                on_file_done(FileEvent(rel_file, "skipped", 0, size, "not a regular file"))
            return

        existing = self.state.get_copy_record(rel_file)
        if existing is not None and existing.status == "success" and dest_abs.exists():
            try:
                if dest_abs.stat().st_size == size:
                    self._write_manifest(rel_file, name, live_stat=st)
                    if on_file_done:
                        on_file_done(FileEvent(rel_file, "already_done", size, size, None))
                    return
            except OSError:
                pass

        self.state.upsert_pending(rel_file, "file", size, mtime)
        self.state.mark_in_progress(rel_file)
        if on_file_start:
            on_file_start(rel_file)

        def progress_cb(n: int) -> None:
            self.state.mark_progress_bytes(rel_file, n)
            if on_file_progress:
                on_file_progress(rel_file, n)

        def should_abort() -> bool:
            return stop_event.is_set()

        dest_abs.parent.mkdir(parents=True, exist_ok=True)

        outcome = self.supervisor.copy_file(
            src_abs, str(dest_abs), progress_cb=progress_cb, should_abort=should_abort
        )

        if outcome.status == "success":
            try:
                os.utime(dest_abs, (atime, mtime))
            except OSError:
                pass
            self.state.mark_success(rel_file, outcome.bytes_copied)
            self._write_manifest(rel_file, name, live_stat=st)
            if on_file_done:
                on_file_done(FileEvent(rel_file, "success", outcome.bytes_copied, size, None))
        elif outcome.status == "aborted":
            self.state.mark_skipped(rel_file, "aborted by user")
            self._write_manifest(rel_file, name, live_stat=st)
            if on_file_done:
                on_file_done(FileEvent(rel_file, "aborted", outcome.bytes_copied, size, None))
        else:
            self.state.mark_failed(rel_file, outcome.error or "unknown error", outcome.bytes_copied)
            self._write_manifest(rel_file, name, live_stat=st)
            if on_file_done:
                on_file_done(
                    FileEvent(rel_file, "failed", outcome.bytes_copied, size, outcome.error)
                )

    def _copy_symlink(
        self, rel_file: str, name: str, src_abs: str, dest_abs: Path, on_file_done
    ) -> None:
        try:
            target = os.readlink(src_abs)
            if dest_abs.exists() or dest_abs.is_symlink():
                dest_abs.unlink()
            os.symlink(target, dest_abs)
            self.state.mark_success(rel_file, 0)
            self._write_manifest(rel_file, name, kind="symlink")
            if on_file_done:
                on_file_done(FileEvent(rel_file, "success", 0, 0, None))
        except OSError as exc:
            self.state.mark_failed(rel_file, f"symlink copy failed: {exc}")
            self._write_manifest(rel_file, name, kind="symlink")
            if on_file_done:
                on_file_done(FileEvent(rel_file, "failed", 0, 0, str(exc)))

    def _fix_directory_timestamps(self) -> None:
        for rec in self.state.records_under(""):
            if rec.kind != "dir" or rec.status == "success" or rec.mtime is None:
                continue
            dest_dir = self.dest_root / rec.path
            if not dest_dir.is_dir():
                continue
            try:
                os.utime(dest_dir, (rec.mtime, rec.mtime))
                self.state.mark_success(rec.path, 0)
            except OSError as exc:
                self.state.mark_failed(rec.path, f"utime failed: {exc}")


def _is_symlink(path: str) -> bool:
    try:
        return os.path.islink(path)
    except OSError:
        return False


def _wait_while_paused(pause_event: threading.Event, stop_event: threading.Event) -> None:
    while pause_event.is_set() and not stop_event.is_set():
        time.sleep(0.2)
