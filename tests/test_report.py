import os
import threading

import yaml

from salvage.copier import CopyEngine
from salvage.report import build_tree_report, dump_tree_yaml
from salvage.selection import SelectionStore
from salvage.state_db import StateDB
from salvage.worker import CopySupervisor


def make_tree(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_bytes(b"hello world")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_bytes(b"nested contents")
    (src / "sub" / "untouched.txt").write_bytes(b"never selected")
    return src, dst


def run_copy(src, dst, tmp_path, manifest_root=None, select_all=True):
    db = StateDB(tmp_path / "state.sqlite3", src, dst)
    selection = SelectionStore(db)
    if select_all:
        selection.set("a.txt", True, is_dir=False)
        selection.set("sub/b.txt", True, is_dir=False)
    engine = CopyEngine(
        src, dst, db, selection,
        supervisor=CopySupervisor(stall_timeout=10),
        manifest_root=manifest_root,
    )
    engine.run(threading.Event(), threading.Event())
    engine.stop_and_shutdown()
    return db, selection


def find_node(tree, rel_path):
    if tree.get("path") == rel_path:
        return tree
    for child in tree.get("children", []):
        found = find_node(child, rel_path)
        if found is not None:
            return found
    return None


def test_manifest_stub_written_for_success_and_failure(tmp_path):
    src, dst = make_tree(tmp_path)
    manifest_root = tmp_path / "manifest"

    db, selection = run_copy(src, dst, tmp_path, manifest_root=manifest_root)

    ok_stub = manifest_root / "a.txt.txt"
    assert ok_stub.exists()
    data = yaml.safe_load(ok_stub.read_text())
    assert data["path"] == "a.txt"
    assert data["name"] == "a.txt"
    assert data["kind"] == "file"
    assert data["status"] == "success"
    assert data["size"] == len(b"hello world")
    assert data["bytes_copied"] == len(b"hello world")
    assert "mtime" in data
    assert data.get("error") is None

    b_stub = manifest_root / "sub" / "b.txt.txt"
    assert b_stub.exists()
    data_b = yaml.safe_load(b_stub.read_text())
    assert data_b["status"] == "success"

    # untouched.txt was never selected: no manifest stub for it at all.
    assert not (manifest_root / "sub" / "untouched.txt.txt").exists()

    db.close()


def test_manifest_stub_reflects_failure(tmp_path):
    import pytest

    if os.geteuid() == 0:
        pytest.skip("root bypasses permission checks")

    src, dst = make_tree(tmp_path)
    bad = src / "sub" / "b.txt"
    bad.chmod(0o000)
    manifest_root = tmp_path / "manifest"
    try:
        db, _ = run_copy(src, dst, tmp_path, manifest_root=manifest_root)
    finally:
        bad.chmod(0o644)

    stub = manifest_root / "sub" / "b.txt.txt"
    assert stub.exists()
    data = yaml.safe_load(stub.read_text())
    assert data["status"] == "failed"
    assert "Permission" in data["error"] or "denied" in data["error"].lower()
    db.close()


def test_build_tree_report_covers_unselected_files(tmp_path):
    src, dst = make_tree(tmp_path)
    db, selection = run_copy(src, dst, tmp_path)

    tree = build_tree_report(src, dst, db, selection)
    assert tree["kind"] == "dir"
    assert tree["path"] == ""

    a_node = find_node(tree, "a.txt")
    assert a_node["status"] == "success"
    assert a_node["selected"] is True

    untouched_node = find_node(tree, "sub/untouched.txt")
    assert untouched_node["status"] == "not_selected"
    assert untouched_node["selected"] is False

    sub_node = find_node(tree, "sub")
    assert sub_node["kind"] == "dir"
    child_names = {c["name"] for c in sub_node["children"]}
    assert child_names == {"b.txt", "untouched.txt"}

    db.close()


def test_build_tree_report_selected_but_not_yet_copied_is_pending(tmp_path):
    src, dst = make_tree(tmp_path)
    db = StateDB(tmp_path / "state.sqlite3", src, dst)
    selection = SelectionStore(db)
    selection.set("a.txt", True, is_dir=False)  # selected, but no copy attempted yet

    tree = build_tree_report(src, dst, db, selection)
    a_node = find_node(tree, "a.txt")
    assert a_node["selected"] is True
    assert a_node["status"] == "pending"  # not "not_selected", despite no DB row yet
    db.close()


def test_dump_tree_yaml_round_trips(tmp_path):
    src, dst = make_tree(tmp_path)
    db, selection = run_copy(src, dst, tmp_path)

    output_path = tmp_path / "report.yaml"
    dump_tree_yaml(src, dst, db, selection, output_path)

    assert output_path.exists()
    loaded = yaml.safe_load(output_path.read_text())
    assert loaded["kind"] == "dir"
    a_node = find_node(loaded, "a.txt")
    assert a_node["status"] == "success"
    db.close()
