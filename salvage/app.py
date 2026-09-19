"""The Textual TUI application tying tree, selection, copier and state together."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog, Static

from salvage import report
from salvage.copier import CopyEngine, FileEvent
from salvage.selection import SelectionStore
from salvage.state_db import StateDB
from salvage.tree_widget import SalvageTree
from salvage.worker import CopySupervisor


class SalvageApp(App):
    CSS = """
    #body { height: 1fr; }
    #tree_pane { width: 2fr; border: round $accent; }
    #side_pane { width: 1fr; }
    #progress { height: auto; border: round $accent; padding: 0 1; }
    #log { height: 1fr; border: round $accent; }
    """

    BINDINGS = [
        Binding("c", "start_copy", "Copy"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("r", "retry_failed", "Retry failed"),
        Binding("y", "dump_tree_yaml", "Dump YAML"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(
        self,
        source: Path,
        dest: Path,
        state: StateDB,
        selection: SelectionStore,
        supervisor: CopySupervisor,
        manifest_root: Optional[Path] = None,
    ):
        super().__init__()
        self.source_root = source
        self.dest_root = dest
        self.state = state
        self.selection = selection
        self.engine = CopyEngine(
            source, dest, state, selection, supervisor=supervisor, manifest_root=manifest_root
        )
        self._dumping_tree = False
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.copying = False
        self._current_file = ""
        self._current_file_size = 0
        self._current_file_bytes = 0
        self._last_rate_time = 0.0
        self._last_rate_bytes = 0
        self._rate = 0.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield SalvageTree(self.source_root, self.state, self.selection, id="tree_pane")
            with Vertical(id="side_pane"):
                yield Static(id="progress")
                yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "salvage"
        self.sub_title = f"{self.source_root} -> {self.dest_root}"
        self.query_one("#tree_pane", SalvageTree).focus()
        self._last_rate_bytes = self.state.bytes_done()
        self._last_rate_time = time.time()
        self._refresh_progress()
        self.set_interval(0.5, self._refresh_progress)
        self.log_line(
            "[bold]Ready.[/] space=select  enter=expand  c=copy  p=pause/resume  "
            "r=retry failed  y=dump yaml report  q=quit"
        )

    # ---- actions -------------------------------------------------------------

    def action_start_copy(self) -> None:
        if self.copying:
            self.log_line("[yellow]A copy pass is already running.[/]")
            return
        self.copying = True
        self.stop_event.clear()
        self.pause_event.clear()
        self.log_line("[bold]Starting copy pass...[/]")
        self.run_worker(self._do_copy, thread=True, exclusive=True, group="copy")

    def action_toggle_pause(self) -> None:
        if not self.copying:
            self.log_line("[yellow]Nothing is running.[/]")
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.log_line("[bold]Resumed.[/]")
        else:
            self.pause_event.set()
            self.log_line("[bold]Pausing after the current file...[/]")

    def action_retry_failed(self) -> None:
        n = self.state.reset_failed_to_pending()
        self.query_one("#tree_pane", SalvageTree).rehydrate()
        self.log_line(f"[bold]Requeued {n} previously failed file(s) for retry.[/]")

    def action_dump_tree_yaml(self) -> None:
        if self._dumping_tree:
            self.log_line("[yellow]Already dumping a tree report.[/]")
            return
        self._dumping_tree = True
        output_path = self.dest_root / report.DEFAULT_TREE_YAML_FILENAME
        self.log_line(f"[bold]Dumping full tree report to {output_path}...[/]")
        self.run_worker(
            lambda: self._do_dump_tree_yaml(output_path), thread=True, group="dump"
        )

    def _do_dump_tree_yaml(self, output_path: Path) -> None:
        try:
            report.dump_tree_yaml(
                self.source_root, self.dest_root, self.state, self.selection, output_path
            )
            self.call_from_thread(self.log_line, f"[bold green]Wrote {output_path}[/]")
        except OSError as exc:
            self.call_from_thread(self.log_line, f"[bold red]Tree dump failed:[/] {exc}")
        finally:
            self._dumping_tree = False

    def action_quit_app(self) -> None:
        self.stop_event.set()

        def shutdown_and_exit() -> None:
            self.engine.stop_and_shutdown()
            self.call_from_thread(self.exit)

        if self.copying:
            self.log_line("[bold]Stopping and shutting down...[/]")
            threading.Thread(target=shutdown_and_exit, daemon=True).start()
        else:
            self.engine.stop_and_shutdown()
            self.exit()

    # ---- copy worker (runs in a background thread) ---------------------------

    def _do_copy(self) -> None:
        try:
            self.engine.run(
                self.stop_event,
                self.pause_event,
                on_file_start=self._on_file_start,
                on_file_progress=self._on_file_progress,
                on_file_done=self._on_file_done,
                on_dir_error=self._on_dir_error,
            )
        finally:
            self.call_from_thread(self._on_copy_finished)

    def _on_file_start(self, rel_path: str) -> None:
        def update() -> None:
            self._current_file = rel_path
            self._current_file_bytes = 0
            rec = self.state.get_copy_record(rel_path)
            self._current_file_size = (rec.size or 0) if rec else 0
            self.query_one("#tree_pane", SalvageTree).record_status_change(rel_path, "in_progress")

        self.call_from_thread(update)

    def _on_file_progress(self, rel_path: str, n: int) -> None:
        def update() -> None:
            if rel_path == self._current_file:
                self._current_file_bytes = n

        self.call_from_thread(update)

    def _on_file_done(self, event: FileEvent) -> None:
        def update() -> None:
            tree = self.query_one("#tree_pane", SalvageTree)
            if event.status != "already_done":
                tree.record_status_change(event.path, event.status)

            if event.status == "success":
                self.log_line(f"[green]OK[/]    {event.path}  ({_human(event.bytes_copied)})")
            elif event.status == "failed":
                self.log_line(f"[bold red]FAIL[/]  {event.path} -- {event.error}")
            elif event.status == "aborted":
                self.log_line(f"[yellow]STOP[/]  {event.path} (aborted mid-copy)")
            elif event.status == "skipped":
                self.log_line(f"[yellow]SKIP[/]  {event.path} -- {event.error}")

        self.call_from_thread(update)

    def _on_dir_error(self, rel_path: str, error: str) -> None:
        def update() -> None:
            self.query_one("#tree_pane", SalvageTree).refresh_path_chain(rel_path)
            self.log_line(f"[bold red]DIR FAIL[/] {rel_path} -- {error}")

        self.call_from_thread(update)

    def _on_copy_finished(self) -> None:
        self.copying = False
        self._current_file = ""
        if self.stop_event.is_set():
            self.log_line("[yellow]Copy stopped.[/]")
        else:
            self.log_line("[bold green]Copy pass complete.[/] Press 'r' to retry any failures.")
        self._refresh_progress()

    # ---- progress panel --------------------------------------------------------

    def _refresh_progress(self) -> None:
        counts = self.state.counts()
        done = counts.get("success", 0)
        failed = counts.get("failed", 0)
        skipped = counts.get("skipped", 0)
        pending = counts.get("pending", 0) + counts.get("in_progress", 0)
        bytes_done = self.state.bytes_done()

        now = time.time()
        if now - self._last_rate_time >= 1.0:
            delta_bytes = bytes_done - self._last_rate_bytes
            delta_t = now - self._last_rate_time
            self._rate = delta_bytes / delta_t if delta_t > 0 else 0.0
            self._last_rate_time = now
            self._last_rate_bytes = bytes_done

        state_word = (
            "PAUSED" if self.pause_event.is_set() else ("COPYING" if self.copying else "IDLE")
        )
        lines = [f"[bold]{state_word}[/]"]
        if self._current_file:
            pct = ""
            if self._current_file_size:
                pct = f" ({100 * self._current_file_bytes / self._current_file_size:.0f}%)"
            lines.append(f"File: {self._current_file}{pct}")
            lines.append(
                f"      {_human(self._current_file_bytes)} / {_human(self._current_file_size)}"
            )
        lines.append("")
        lines.append(
            f"[green]Done:[/] {done}   [bold red]Failed:[/] {failed}   "
            f"[yellow]Skipped:[/] {skipped}   Pending: {pending}"
        )
        lines.append(f"Copied: {_human(bytes_done)}   Rate: {_human(self._rate)}/s")

        self.query_one("#progress", Static).update("\n".join(lines))

    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)


def _human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
