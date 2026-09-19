import pytest

from salvage.selection import SelectionStore
from salvage.state_db import StateDB


@pytest.fixture
def store(tmp_path):
    db = StateDB(tmp_path / "state.sqlite3", tmp_path / "src", tmp_path / "dst")
    yield SelectionStore(db)
    db.close()


def test_default_unselected(store):
    assert store.effective("a/b/c.txt") is False


def test_toggle_root_selects_everything(store):
    store.toggle("", is_dir=True)
    assert store.effective("a/b/c.txt") is True
    assert store.effective("anything") is True


def test_directory_include_then_exclude_child(store):
    store.set("photos", True, is_dir=True)
    assert store.effective("photos/a.jpg") is True
    assert store.effective("photos/sub/b.jpg") is True

    store.set("photos/sub", False, is_dir=True)
    assert store.effective("photos/a.jpg") is True
    assert store.effective("photos/sub/b.jpg") is False
    assert store.effective("photos/sub/deeper/c.jpg") is False


def test_reselecting_parent_clears_child_overrides(store):
    store.set("photos", True, is_dir=True)
    store.set("photos/sub", False, is_dir=True)
    assert store.effective("photos/sub/b.jpg") is False

    # Toggling the parent again should wipe the child override.
    store.set("photos", False, is_dir=True)
    store.set("photos", True, is_dir=True)
    assert store.effective("photos/sub/b.jpg") is True
    assert not store.is_explicit("photos/sub")


def test_any_include_below_allows_pruning_decision(store):
    store.set("bigdir", False, is_dir=True)
    assert store.effective("bigdir") is False
    assert store.any_include_below("bigdir") is False

    store.set("bigdir/keepme", True, is_dir=True)
    assert store.effective("bigdir") is False
    assert store.any_include_below("bigdir") is True
    assert store.effective("bigdir/keepme/file.txt") is True
    assert store.effective("bigdir/other/file.txt") is False


def test_single_file_toggle_does_not_affect_siblings(store):
    store.set("dir", True, is_dir=True)
    store.set("dir/skip.txt", False, is_dir=False)
    assert store.effective("dir/skip.txt") is False
    assert store.effective("dir/keep.txt") is True
