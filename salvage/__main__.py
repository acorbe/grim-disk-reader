"""CLI entry point: `salvage <source> <dest>` launches the TUI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from salvage import report
from salvage.app import SalvageApp
from salvage.selection import SelectionStore
from salvage.state_db import RootMismatchError, StateDB
from salvage.worker import DEFAULT_CHUNK_SIZE, DEFAULT_STALL_TIMEOUT, CopySupervisor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="salvage",
        description="Resilient, resumable, TUI-driven file copier for a dying disk.",
    )
    parser.add_argument("source", type=Path, help="Source directory (the failing disk/mount)")
    parser.add_argument("dest", type=Path, help="Destination directory to rebuild the tree into")
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="Path to the SQLite state file (default: <dest>/.salvage-state.sqlite3)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Read/write chunk size in bytes (default: {DEFAULT_CHUNK_SIZE})",
    )
    parser.add_argument(
        "--stall-timeout",
        type=float,
        default=DEFAULT_STALL_TIMEOUT,
        help="Seconds with zero read progress before a file is treated as stuck "
        f"and abandoned so the run can move on (default: {DEFAULT_STALL_TIMEOUT})",
    )
    parser.add_argument(
        "--manifest-dir",
        type=str,
        default=".salvage-manifest",
        help="Directory (relative to dest, unless given as an absolute path) mirroring "
        "the source tree with a small YAML metadata stub (date, size, copy status, "
        "error...) in place of each real file (default: .salvage-manifest). "
        "See --no-manifest to turn this off.",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="Disable the per-file YAML metadata-stub mirror tree.",
    )
    parser.add_argument(
        "--dump-tree-yaml",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Write a single YAML file describing the full source tree (including "
        "files that were never selected) with each entry's current copy status, "
        f"then exit without launching the TUI. PATH defaults to <dest>/"
        f"{report.DEFAULT_TREE_YAML_FILENAME} if omitted. The same report is also "
        "reachable from inside the TUI with the 'y' key.",
    )
    args = parser.parse_args(argv)

    source = args.source.resolve()
    dest = args.dest.resolve()

    if not source.is_dir():
        parser.error(f"source is not a directory: {source}")

    try:
        source.relative_to(dest)
        parser.error("source must not be the same as, or inside, dest")
    except ValueError:
        pass
    try:
        dest.relative_to(source)
        parser.error("dest must not be the same as, or inside, source (it would copy into itself)")
    except ValueError:
        pass

    dest.mkdir(parents=True, exist_ok=True)
    state_db_path = args.state_db or (dest / ".salvage-state.sqlite3")

    try:
        state = StateDB(state_db_path, source, dest)
    except RootMismatchError as exc:
        parser.error(str(exc))

    selection = SelectionStore(state)

    if args.dump_tree_yaml is not None:
        output_path = (
            Path(args.dump_tree_yaml)
            if args.dump_tree_yaml
            else dest / report.DEFAULT_TREE_YAML_FILENAME
        )
        try:
            report.dump_tree_yaml(source, dest, state, selection, output_path)
        finally:
            state.close()
        print(f"Wrote {output_path}")
        return 0

    manifest_root = None
    if not args.no_manifest:
        manifest_dir = Path(args.manifest_dir)
        manifest_root = manifest_dir if manifest_dir.is_absolute() else dest / manifest_dir

    supervisor = CopySupervisor(chunk_size=args.chunk_size, stall_timeout=args.stall_timeout)

    app = SalvageApp(source, dest, state, selection, supervisor, manifest_root=manifest_root)
    try:
        app.run()
    finally:
        state.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
