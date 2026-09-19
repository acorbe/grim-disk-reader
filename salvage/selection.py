"""Recursive include/exclude selection over a directory tree.

Selection is stored as a sparse set of explicit overrides (one per path the
user actually toggled), not as a flag per file -- that would be too expensive
to maintain over a tree with millions of entries. The *effective* state of
any path is resolved by walking up from that path to the root and taking the
nearest ancestor (inclusive) that has an explicit override. With no override
anywhere on the path, the default is "not selected".

Toggling a directory clears any overrides strictly beneath it (they are now
superseded by the new blanket decision) and installs a single override at
that directory. Toggling a file only ever touches that file's own override.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from salvage.state_db import StateDB, to_rel

INCLUDE = "include"
EXCLUDE = "exclude"


class SelectionStore:
    def __init__(self, db: StateDB):
        self.db = db

    def effective(self, path: str) -> bool:
        """Is `path` selected for copy, considering inherited ancestor state?"""
        path = to_rel(path)
        parts = PurePosixPath(path).parts if path else ()
        # Walk from the path itself up to the root; nearest override wins.
        for depth in range(len(parts), -1, -1):
            candidate = "/".join(parts[:depth])
            override = self.db.get_selection_override(candidate)
            if override is not None:
                return override == INCLUDE
        return False

    def is_explicit(self, path: str) -> bool:
        return self.db.get_selection_override(path) is not None

    def any_include_below(self, path: str) -> bool:
        """True if some descendant of `path` was individually re-included,
        even though `path` itself resolves to excluded."""
        return self.db.has_override_under(path, INCLUDE)

    def any_exclude_below(self, path: str) -> bool:
        """True if some descendant of `path` was individually excluded,
        even though `path` itself resolves to included (for tri-state checkboxes)."""
        return self.db.has_override_under(path, EXCLUDE)

    def toggle(self, path: str, is_dir: bool) -> bool:
        """Flip effective selection at `path`. Returns the new state (True=included)."""
        new_state = not self.effective(path)
        self.set(path, new_state, is_dir)
        return new_state

    def set(self, path: str, included: bool, is_dir: bool) -> None:
        path = to_rel(path)
        if is_dir:
            self.db.clear_selection_overrides_under(path)
        self.db.set_selection_override(path, INCLUDE if included else EXCLUDE)

    def child_summary(self, dir_path: str, child_names: list[str]) -> str:
        """For rendering a directory checkbox: 'all', 'none', or 'mixed'.

        Only meaningful as a hint for immediate children actually listed;
        the effective() of the directory itself is the source of truth for
        whether new/unseen children would be included.
        """
        dir_path = to_rel(dir_path)
        states = [
            self.effective(f"{dir_path}/{name}" if dir_path else name)
            for name in child_names
        ]
        if not states:
            return "none"
        if all(states):
            return "all"
        if not any(states):
            return "none"
        return "mixed"
