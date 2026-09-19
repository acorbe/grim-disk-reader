"""Headless end-to-end smoke test for the TUI: select via keypress, copy, verify
that both the destination tree and the in-memory status coloring are correct,
including a deliberately unreadable file to exercise the red/failed path.
"""

import asyncio
import os

import pytest

from salvage.app import SalvageApp
from salvage.selection import SelectionStore
from salvage.state_db import StateDB
from salvage.tree_widget import SalvageTree
from salvage.worker import CopySupervisor


def _make_app(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_bytes(b"hello")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_bytes(b"world")
    (src / "sub" / "bad.txt").write_bytes(b"unreadable")
    (src / "sub" / "bad.txt").chmod(0o000)

    state = StateDB(tmp_path / "state.sqlite3", src, dst)
    selection = SelectionStore(state)
    supervisor = CopySupervisor(stall_timeout=10)
    app = SalvageApp(src, dst, state, selection, supervisor)
    return app, state, src, dst


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses read permission checks")
def test_tui_select_copy_and_status_colors(tmp_path):
    async def scenario():
        app, state, src, dst = _make_app(tmp_path)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()

                tree = app.query_one("#tree_pane", SalvageTree)
                tree.focus()
                await pilot.pause()

                await pilot.press("space")  # toggle root -> select everything
                await pilot.pause()
                assert app.selection.effective("a.txt") is True
                assert app.selection.effective("sub/bad.txt") is True

                await pilot.press("c")  # start copy
                for _ in range(200):
                    await pilot.pause(0.05)
                    if not app.copying:
                        break
                assert not app.copying, "copy pass did not finish in time"

                assert tree._file_status["a.txt"] == "success"
                assert tree._file_status["sub/b.txt"] == "success"
                assert tree._file_status["sub/bad.txt"] == "failed"
                assert tree.agg[""]["failed"] == 1
                assert tree.agg["sub"]["failed"] == 1

                # fix permissions and retry the failed file via the 'r' key
                (src / "sub" / "bad.txt").chmod(0o644)
                await pilot.press("r")
                await pilot.pause()
                assert tree._file_status["sub/bad.txt"] == "pending"

                await pilot.press("c")
                for _ in range(200):
                    await pilot.pause(0.05)
                    if not app.copying:
                        break
                assert tree._file_status["sub/bad.txt"] == "success"
                assert tree.agg[""]["failed"] == 0
        finally:
            try:
                (src / "sub" / "bad.txt").chmod(0o644)
            except OSError:
                pass
            app.engine.stop_and_shutdown()
            state.close()

        assert (dst / "a.txt").read_bytes() == b"hello"
        assert (dst / "sub" / "b.txt").read_bytes() == b"world"
        assert (dst / "sub" / "bad.txt").read_bytes() == b"unreadable"

    asyncio.run(scenario())
