"""A killable subprocess that does the actual file copying.

Why a subprocess and not a thread: on a dying drive, a read() into a bad
sector can block in the kernel for a long time (firmware ECC retries) or,
in the worst case, effectively forever until a power cycle. Python cannot
forcibly abort a thread stuck in a blocking syscall. A subprocess *can* be
SIGKILLed by the parent, which is the only reliable way to get unstuck.

The supervisor keeps one long-lived worker process alive across many files
(cheap for the common case of many small/medium files) and only pays the
cost of respawning when a copy actually wedges or the worker dies.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import time
from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
DEFAULT_STALL_TIMEOUT = 20.0  # seconds with zero read progress before we call it stuck
POLL_INTERVAL = 1.0

_STOP = ("stop",)


def _tmp_path(dest: str) -> str:
    return dest + ".salvage-tmp"


def _worker_main(task_q: "mp.Queue", result_q: "mp.Queue") -> None:
    while True:
        try:
            task = task_q.get()
        except (EOFError, OSError):
            return
        if task == _STOP:
            return
        _kind, src, dest, chunk_size = task
        tmp = _tmp_path(dest)
        total = 0
        try:
            with open(src, "rb", buffering=0) as fsrc, open(tmp, "wb", buffering=0) as fdst:
                while True:
                    chunk = fsrc.read(chunk_size)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    total += len(chunk)
                    result_q.put(("progress", total))
            os.replace(tmp, dest)
            result_q.put(("done", total))
        except Exception as exc:  # noqa: BLE001 - report every failure, never crash the loop
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            result_q.put(("error", total, f"{type(exc).__name__}: {exc}"))


@dataclass
class CopyOutcome:
    status: str  # 'success' or 'failed'
    bytes_copied: int
    error: Optional[str]


class CopySupervisor:
    """Owns one worker process at a time; kills and replaces it on a stall."""

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        stall_timeout: float = DEFAULT_STALL_TIMEOUT,
    ):
        self.chunk_size = chunk_size
        self.stall_timeout = stall_timeout
        # "fork" (not "spawn"): the supervisor spawns/respawns worker processes
        # from a background thread while Textual's asyncio loop is running on
        # the main thread. "spawn" re-execs a fresh interpreter and registers
        # POSIX semaphores with the resource tracker via its own subprocess
        # launch, and doing that concurrently from a non-main thread races
        # with the event loop over file descriptors (observed as a bogus
        # "bad value(s) in fds_to_keep" from posix_spawn). "fork" avoids the
        # re-exec/tracker-registration path entirely and is safe here since
        # the forked child only ever does plain file I/O, never touching
        # asyncio/Textual state inherited from the parent's other threads.
        self._ctx = mp.get_context("fork")
        self._task_q: Optional["mp.Queue"] = None
        self._result_q: Optional["mp.Queue"] = None
        self._proc: Optional[mp.process.BaseProcess] = None

    def start(self) -> None:
        self._spawn()

    def _spawn(self) -> None:
        self._task_q = self._ctx.Queue()
        self._result_q = self._ctx.Queue()
        self._proc = self._ctx.Process(
            target=_worker_main, args=(self._task_q, self._result_q), daemon=True
        )
        self._proc.start()

    def _kill(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=2)
            if self._proc.is_alive():
                self._proc.kill()
                self._proc.join(timeout=2)
        for q in (self._task_q, self._result_q):
            if q is not None:
                q.close()

    def _kill_and_respawn(self, dest: str) -> None:
        self._kill()
        try:
            tmp = _tmp_path(dest)
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        self._spawn()

    def shutdown(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            try:
                self._task_q.put(_STOP)
                self._proc.join(timeout=3)
            except (OSError, ValueError):
                pass
        self._kill()

    def copy_file(
        self,
        src: str,
        dest: str,
        progress_cb: Optional[Callable[[int], None]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> CopyOutcome:
        if self._proc is None or not self._proc.is_alive():
            self._spawn()

        self._task_q.put(("copy", src, dest, self.chunk_size))
        last_bytes = 0
        deadline = time.monotonic() + self.stall_timeout
        while True:
            if should_abort is not None and should_abort():
                self._kill_and_respawn(dest)
                return CopyOutcome("aborted", last_bytes, "aborted by user")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_and_respawn(dest)
                return CopyOutcome(
                    "failed",
                    last_bytes,
                    f"stalled: no read progress for {self.stall_timeout:.0f}s "
                    "(likely a bad sector or the drive hanging)",
                )
            try:
                msg = self._result_q.get(timeout=min(POLL_INTERVAL, remaining))
            except queue.Empty:
                if not self._proc.is_alive():
                    self._kill_and_respawn(dest)
                    return CopyOutcome(
                        "failed", last_bytes, "worker process died unexpectedly"
                    )
                continue

            kind = msg[0]
            if kind == "progress":
                last_bytes = msg[1]
                if progress_cb:
                    progress_cb(last_bytes)
                deadline = time.monotonic() + self.stall_timeout
            elif kind == "done":
                return CopyOutcome("success", msg[1], None)
            elif kind == "error":
                return CopyOutcome("failed", msg[1], msg[2])
