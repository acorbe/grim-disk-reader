"""A lazily-loaded, checkbox-driven Tree widget over the source filesystem.

Directory checkboxes are tri-state (unselected / selected / partial) and
every node's name is colored by its current copy status, aggregated up from
descendants for directories. Aggregation is kept in an in-memory counter
table (`self.agg`) updated incrementally as copy events arrive, rather than
re-querying SQLite on every render -- that keeps the tree responsive even
over a source tree with hundreds of thousands of entries.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from salvage.selection import SelectionStore
from salvage.state_db import StateDB

STATUS_KEYS = ("pending", "in_progress", "success", "failed", "skipped")

CHECK_UNSELECTED = "☐"  # ☐
CHECK_SELECTED = "☑"  # ☑
CHECK_PARTIAL = "▪"  # ▪

ARROW_EXPANDED = "▼"  # matches Tree.ICON_NODE_EXPANDED
ARROW_COLLAPSED = "▶"  # matches Tree.ICON_NODE


@dataclass
class NodeInfo:
    rel_path: str
    name: str
    is_dir: bool
    populated: bool = False


def ancestors_of(rel_path: str) -> list[str]:
    """Directory ancestors of `rel_path`, root ("") first, not including rel_path itself."""
    if not rel_path:
        return []
    parts = rel_path.split("/")
    out = [""]
    for i in range(1, len(parts)):
        out.append("/".join(parts[:i]))
    return out


class SalvageTree(Tree[NodeInfo]):
    # Tree's own binding maps space -> expand/collapse ("toggle_node"), which
    # would shadow the app-level selection-toggle binding on the same key
    # (the focused widget's bindings win). Re-map it to our checkbox action;
    # expand/collapse remains available via enter (and mouse click).
    BINDINGS = [
        Binding("space", "toggle_check", "Select/Deselect", show=False),
    ]

    def __init__(self, source_root: Path, state: StateDB, selection: SelectionStore, **kwargs):
        super().__init__(source_root.name or str(source_root), **kwargs)
        self.source_root = source_root
        self.state = state
        self.selection = selection
        self.agg: dict[str, Counter] = {}
        self._file_status: dict[str, str] = {}
        self._nodes_by_path: dict[str, TreeNode[NodeInfo]] = {}
        self.show_root = True
        self.guide_depth = 3

    def on_mount(self) -> None:
        self.root.data = NodeInfo("", self.source_root.name or "/", True)
        self._nodes_by_path[""] = self.root
        self.rehydrate()
        self._populate_children(self.root)
        self.root.expand()

    # ---- cache-busting: Tree's own line cache is keyed on a per-node update
    # counter (see `TreeNode.refresh`), not on any of our external selection/
    # status state -- widget-level `self.refresh()` schedules a repaint but
    # does NOT bump that counter, so a previously-rendered line is silently
    # reused verbatim. Every path below that changes what a node should look
    # like must explicitly call `.refresh()` on the affected TreeNode(s).

    def _refresh_node(self, rel_path: str) -> None:
        node = self._nodes_by_path.get(rel_path)
        if node is not None:
            node.refresh()

    def refresh_path_chain(self, rel_path: str) -> None:
        """Refresh `rel_path`'s own (loaded) node plus all of its ancestors."""
        self._refresh_node(rel_path)
        for ancestor in ancestors_of(rel_path):
            self._refresh_node(ancestor)

    def _refresh_subtree(self, rel_path: str) -> None:
        """Refresh `rel_path`'s own node plus every already-loaded descendant."""
        prefix = rel_path + "/" if rel_path else ""
        self._refresh_node(rel_path)
        for path, node in self._nodes_by_path.items():
            if prefix and path.startswith(prefix):
                node.refresh()

    # ---- aggregate status bookkeeping --------------------------------------

    def rehydrate(self) -> None:
        """(Re)build the in-memory aggregate cache from the state DB.

        Cheap to call after a bulk DB mutation the incremental path doesn't
        cover (e.g. retry-failed resetting many rows at once) since it's a
        single grouped scan, not a per-render cost.
        """
        self.agg.clear()
        self._file_status.clear()
        for rel, rec in self.state.all_copy_records().items():
            if rec.kind != "file":
                continue
            self._bump_ancestors(rel, rec.status, +1)
            self._file_status[rel] = rec.status
        for path in self._nodes_by_path:
            self._refresh_node(path)

    def _bump_ancestors(self, rel_file: str, status: str, delta: int) -> None:
        for ancestor in ancestors_of(rel_file):
            counter = self.agg.setdefault(ancestor, Counter())
            counter[status] += delta

    def record_status_change(self, rel_file: str, new_status: str) -> None:
        """Transition `rel_file` to `new_status`, deriving the prior status
        from what we last counted it as (never guessed by the caller -- that
        was a source of drift when a file was brand new or had been bulk-reset)."""
        old_status = self._file_status.get(rel_file)
        if old_status == new_status:
            return
        if old_status is not None:
            self._bump_ancestors(rel_file, old_status, -1)
        self._bump_ancestors(rel_file, new_status, +1)
        self._file_status[rel_file] = new_status
        self.refresh_path_chain(rel_file)

    def _dir_style(self, rel_path: str) -> str | None:
        own = self.state.get_copy_record(rel_path)
        if own is not None and own.kind == "dir" and own.status == "failed":
            # e.g. the directory itself couldn't be listed (permission/I-O
            # error during the walk) -- not reflected in the file aggregate.
            return "bold red"
        counter = self.agg.get(rel_path)
        if not counter or sum(counter.values()) == 0:
            return None
        if counter["failed"] > 0:
            return "bold red"
        if counter["in_progress"] > 0:
            return "bold cyan"
        if counter["pending"] > 0:
            return "yellow"
        if counter["skipped"] > 0:
            return "yellow"
        return "green"

    def _file_style(self, rel_path: str) -> str | None:
        rec = self.state.get_copy_record(rel_path)
        if rec is None:
            return None
        return {
            "pending": None,
            "in_progress": "bold cyan",
            "success": "green",
            "failed": "bold red",
            "skipped": "yellow",
        }.get(rec.status)

    # ---- selection ----------------------------------------------------------

    def _checkbox_glyph(self, data: NodeInfo) -> str:
        if not data.is_dir:
            return CHECK_SELECTED if self.selection.effective(data.rel_path) else CHECK_UNSELECTED
        eff = self.selection.effective(data.rel_path)
        if eff:
            return CHECK_PARTIAL if self.selection.any_exclude_below(data.rel_path) else CHECK_SELECTED
        return CHECK_PARTIAL if self.selection.any_include_below(data.rel_path) else CHECK_UNSELECTED

    def toggle_selection(self, node: TreeNode[NodeInfo]) -> None:
        data = node.data
        if data is None:
            return
        self.selection.toggle(data.rel_path, data.is_dir)
        # Descendants inherit this node's new state (checkbox changes for all
        # of them); ancestors may flip to/from the tri-state "partial" glyph.
        self._refresh_subtree(data.rel_path)
        for ancestor in ancestors_of(data.rel_path):
            self._refresh_node(ancestor)

    def action_toggle_check(self) -> None:
        if self.cursor_node is not None:
            self.toggle_selection(self.cursor_node)

    # ---- lazy population ------------------------------------------------------

    def _populate_children(self, node: TreeNode[NodeInfo]) -> None:
        data = node.data
        if data is None or data.populated:
            return
        abs_path = self.source_root if not data.rel_path else self.source_root / data.rel_path
        try:
            entries = list(os.scandir(abs_path))
        except OSError as exc:
            node.add_leaf(f"[error reading directory: {exc}]")
            data.populated = True
            return

        def sort_key(e: os.DirEntry) -> tuple[bool, str]:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            return (not is_dir, e.name.lower())

        entries.sort(key=sort_key)
        for entry in entries:
            child_rel = f"{data.rel_path}/{entry.name}" if data.rel_path else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            child_data = NodeInfo(child_rel, entry.name, is_dir)
            if is_dir:
                child_node = node.add(entry.name, data=child_data, allow_expand=True)
            else:
                child_node = node.add_leaf(entry.name, data=child_data)
            self._nodes_by_path[child_rel] = child_node
        data.populated = True

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        data = event.node.data
        if data is not None and data.is_dir and not data.populated:
            self._populate_children(event.node)

    # ---- rendering ------------------------------------------------------------

    def render_label(self, node: TreeNode[NodeInfo], base_style: Style, style: Style) -> Text:
        data = node.data
        if data is None:
            return super().render_label(node, base_style, style)

        arrow = "  "
        if node.allow_expand:
            arrow = (ARROW_EXPANDED if node.is_expanded else ARROW_COLLAPSED) + " "

        checkbox = self._checkbox_glyph(data)
        name = data.name + ("/" if data.is_dir else "")
        status_style = self._dir_style(data.rel_path) if data.is_dir else self._file_style(data.rel_path)

        # Combine explicitly rather than Text.stylize()-ing the whole label
        # afterwards: stylize() layers `style` as a span on top of everything,
        # which was clobbering the per-status color set on `name` (status
        # colors silently never showed up in the tree, though the log panel,
        # built differently, was fine). Style.__add__ lets the right-hand
        # side's set attributes win, so folding status_style into `style`
        # ourselves keeps cursor/selection highlighting *and* the status color.
        name_style = style + Style.parse(status_style) if status_style else style

        text = Text()
        text.append(arrow, style=base_style)
        text.append(checkbox + " ", style=style)
        text.append(name, style=name_style)
        return text
