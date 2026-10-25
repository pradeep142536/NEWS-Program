from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a bot database backup to a new path")
    parser.add_argument("backup", type=Path)
    parser.add_argument("destination", type=Path, help="new path; an existing file is never overwritten")
    args = parser.parse_args()
    if not args.backup.is_file():
        raise SystemExit(f"Backup file does not exist: {args.backup}")
    if args.destination.exists():
        raise SystemExit(f"Destination already exists; choose a new path: {args.destination}")
    source = sqlite3.connect(f"file:{args.backup.resolve().as_posix()}?mode=ro", uri=True)
    target = None
    try:
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"Backup integrity check failed: {integrity}")
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(args.destination))
        source.backup(target)
        restored_integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if restored_integrity != "ok":
            raise SystemExit(f"Restored database integrity check failed: {restored_integrity}")
    finally:
        if target is not None:
            target.close()
        source.close()
    print(f"Restored and integrity checked to new file: {args.destination}")


if __name__ == "__main__":
    main()
