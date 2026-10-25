from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    source = Path(os.environ.get("BOT_DATABASE", "data/intelligence.sqlite3"))
    target_dir = Path(os.environ.get("BOT_BACKUP_DIR", "backups"))
    if not source.is_absolute():
        source = Path(__file__).resolve().parents[1] / source
    if not source.is_file():
        raise SystemExit(f"Database does not exist: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = target_dir / f"intelligence-{stamp}.sqlite3"
    db = sqlite3.connect(str(source), timeout=30)
    backup = sqlite3.connect(str(destination))
    try:
        db.backup(backup)
        result = backup.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
    finally:
        backup.close()
        db.close()
    print(f"Backup created and integrity checked: {destination}")


if __name__ == "__main__":
    main()
