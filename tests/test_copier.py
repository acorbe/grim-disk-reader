import os
import time

import pytest

from salvage.copier import CopyEngine
from salvage.selection import SelectionStore
from salvage.state_db import StateDB
from salvage.worker import CopySupervisor

import threading


def run_engine(engine):
    events = []
    engine.run(
        threading.Event(),
        threading.Event(),
        on_file_done=lambda ev: events.append(ev),
    )
    return events


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_bytes(b"hello world")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_bytes(b"nested file contents")
    (src / "sub" / "deep").mkdir()
    (src / "sub" / "deep" / "c.txt").write_bytes(b"deep file")

    old_time = time.time() - 10_000
    for p in [
        src / "a.txt",
        src / "sub",
        src / "sub" / "b.txt",
        src / "sub" / "deep",
        src / "sub" / "deep" / "c.txt",
    ]:
        os.utime(p, (old_time, old_time))

    return src, dst


def make_engine(tmp_path, src, dst, select_all=True):
    db = StateDB(tmp_path / "state.sqlite3", src, dst)
    selection = SelectionStore(db)
    if select_all:
        selection.set("", True, is_dir=True)
    engine = CopyEngine(src, dst, db, selection, supervisor=CopySupervisor(stall_timeout=10))
    return engine, db, selection


def test_basic_copy_preserves_mtime_and_content(tmp_path, tree):
    src, dst = tree
    engine, db, _ = make_engine(tmp_path, src, dst)
    events = run_engine(engine)
    engine.stop_and_shutdown()

    assert all(e.status == "success" for e in events)
    assert (dst / "a.txt").read_bytes() == b"hello world"
    assert (dst / "sub" / "b.txt").read_bytes() == b"nested file contents"
    assert (dst / "sub" / "deep" / "c.txt").read_bytes() == b"deep file"

    for rel in ["a.txt", "sub/b.txt", "sub/deep/c.txt"]:
        src_mtime = (src / rel).stat().st_mtime
        dst_mtime = (dst / rel).stat().st_mtime
        assert abs(src_mtime - dst_mtime) < 1.0

    # Directory timestamps are fixed up at the end too.
    assert abs((src / "sub").stat().st_mtime - (dst / "sub").stat().st_mtime) < 1.0
    assert abs(
        (src / "sub" / "deep").stat().st_mtime - (dst / "sub" / "deep").stat().st_mtime
    ) < 1.0

    counts = db.counts()
    assert counts.get("success") == 3


def test_excluded_subtree_is_not_copied(tmp_path, tree):
    src, dst = tree
    engine, db, selection = make_engine(tmp_path, src, dst)
    selection.set("sub/deep", False, is_dir=True)

    run_engine(engine)
    engine.stop_and_shutdown()

    assert (dst / "a.txt").exists()
    assert (dst / "sub" / "b.txt").exists()
    assert not (dst / "sub" / "deep").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses read permission checks")
def test_unreadable_file_fails_but_others_succeed(tmp_path, tree):
    src, dst = tree
    bad = src / "sub" / "b.txt"
    bad.chmod(0o000)
    try:
        engine, db, _ = make_engine(tmp_path, src, dst)
        events = run_engine(engine)
        engine.stop_and_shutdown()
    finally:
        bad.chmod(0o644)

    by_path = {e.path: e for e in events}
    assert by_path["a.txt"].status == "success"
    assert by_path["sub/deep/c.txt"].status == "success"
    assert by_path["sub/b.txt"].status == "failed"
    assert "Permission" in by_path["sub/b.txt"].error or "denied" in by_path["sub/b.txt"].error.lower()

    rec = db.get_copy_record("sub/b.txt")
    assert rec.status == "failed"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses read permission checks")
def test_resume_skips_success_and_retries_failed(tmp_path, tree):
    src, dst = tree
    bad = src / "sub" / "b.txt"
    bad.chmod(0o000)

    engine, db, selection = make_engine(tmp_path, src, dst)
    run_engine(engine)
    engine.stop_and_shutdown()
    assert db.get_copy_record("sub/b.txt").status == "failed"
    assert db.get_copy_record("a.txt").status == "success"

    bad.chmod(0o644)
    reset_count = db.reset_failed_to_pending()
    assert reset_count == 1

    engine2 = CopyEngine(src, dst, db, selection, supervisor=CopySupervisor(stall_timeout=10))
    events2 = run_engine(engine2)
    engine2.stop_and_shutdown()

    by_path = {e.path: e for e in events2}
    # a.txt was already successful and should be reported as unchanged, not recopied.
    assert by_path["a.txt"].status == "already_done"
    # the previously-failed file should now succeed.
    assert by_path["sub/b.txt"].status == "success"
    assert (dst / "sub" / "b.txt").read_bytes() == b"nested file contents"
