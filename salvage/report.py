"""Human-readable per-file metadata stubs, and a full-tree YAML status dump.

Both features build on the same `file_record()` shape so a manifest stub and
the corresponding entry in the full tree dump always agree with each other
(and with what the live TUI shows).
"""

from __future__ import annotations

import os
import stat as stat_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from salvage.selection import SelectionStore
from salvage.state_db import StateDB

MANIFEST_SUFFIX = ".txt"
DEFAULT_TREE_YAML_FILENAME = "salvage-tree-report.yaml"


def to_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def file_record(
    rel_path: str,
    name: str,
    state: StateDB,
    dest_root: Path,
    live_stat: Optional[os.stat_result] = None,
    kind: str = "file",
) -> dict:
    """Metadata for one file: whatever's persisted in the state DB (status,
    attempts, timestamps, error), merged with a freshly-taken stat if one is
    available (e.g. right after reading the source during a copy attempt)."""
    data: dict = {"path": rel_path, "name": name, "kind": kind}

    if live_stat is not None:
        data["size"] = live_stat.st_size
        data["mtime"] = to_iso(live_stat.st_mtime)
        # Linux doesn't expose a file's creation time through the standard
        # stat() call (it would need the statx() syscall directly) -- only
        # fill this in where the platform actually provides it, e.g. macOS/BSD.
        birthtime = getattr(live_stat, "st_birthtime", None)
        if birthtime is not None:
            data["created"] = to_iso(birthtime)

    rec = state.get_copy_record(rel_path)
    if rec is not None:
        data.setdefault("size", rec.size)
        data.setdefault("mtime", to_iso(rec.mtime))
        data["status"] = rec.status
        data["bytes_copied"] = rec.bytes_copied
        data["attempts"] = rec.attempts
        data["last_attempt"] = to_iso(rec.last_attempt_ts)
        data["last_success"] = to_iso(rec.last_success_ts)
        if rec.error:
            data["error"] = rec.error
    else:
        data["status"] = "not_selected"

    data["dest_exists"] = (dest_root / rel_path).exists()
    return data


def write_manifest_stub(manifest_root: Path, rel_path: str, data: dict) -> None:
    """Write `data` as a small YAML text stub, mirroring `rel_path` under
    `manifest_root` with `.txt` appended so it's visually obvious it's a
    metadata stand-in, not the real file."""
    stub_path = manifest_root / (rel_path + MANIFEST_SUFFIX)
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    stub_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def build_tree_report(
    source_root: Path,
    dest_root: Path,
    state: StateDB,
    selection: SelectionStore,
) -> dict:
    """Walk the *live* source tree (not just the state DB, which only knows
    about files that were actually selected) and annotate every entry with
    its current selection/copy status -- including files never selected at
    all, which come back as status 'not_selected'."""

    def walk(abs_path: Path, rel_path: str) -> dict:
        name = abs_path.name or (source_root.name or str(source_root))
        try:
            st = os.lstat(abs_path)
        except OSError as exc:
            return {"path": rel_path, "name": name, "kind": "unknown", "error": str(exc)}

        if stat_module.S_ISLNK(st.st_mode):
            node = file_record(rel_path, name, state, dest_root, kind="symlink")
            _finalize_selection_status(node, selection.effective(rel_path))
            return node

        if stat_module.S_ISDIR(st.st_mode):
            node = {
                "path": rel_path,
                "name": name,
                "kind": "dir",
                "dest_exists": (dest_root / rel_path).exists() if rel_path else True,
            }
            try:
                entries = sorted(os.scandir(abs_path), key=lambda e: e.name.lower())
            except OSError as exc:
                node["error"] = str(exc)
                entries = []
            node["children"] = [
                walk(Path(e.path), f"{rel_path}/{e.name}" if rel_path else e.name)
                for e in entries
            ]
            return node

        node = file_record(rel_path, name, state, dest_root, live_stat=st)
        _finalize_selection_status(node, selection.effective(rel_path))
        return node

    return walk(source_root, "")


def _finalize_selection_status(node: dict, selected: bool) -> None:
    """`file_record` reports status 'not_selected' whenever there's no copy_state
    row yet -- true for a genuinely unselected file, but also true for one
    that's selected and simply hasn't had its first copy attempt yet. Correct
    that second case to 'pending' so `status` and `selected` never disagree."""
    node["selected"] = selected
    if node["status"] == "not_selected" and selected:
        node["status"] = "pending"


def dump_tree_yaml(
    source_root: Path,
    dest_root: Path,
    state: StateDB,
    selection: SelectionStore,
    output_path: Path,
) -> None:
    tree = build_tree_report(source_root, dest_root, state, selection)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(tree, sort_keys=False, allow_unicode=True))
