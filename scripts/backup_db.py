"""Snapshot the OVERLAP SQLite database to a timestamped backup file.

Uses SQLite's online backup API, so the snapshot is consistent even
while the service is running (no need to stop uvicorn).

Configuration (environment):
- OVERLAP_DB_PATH    source database (default: overlap.db)
- OVERLAP_BACKUP_DIR where snapshots go (default: backups/)

Run:  python scripts/backup_db.py
Prints the path of the snapshot it wrote. Copy snapshots off the host
regularly — a backup on the same disk as the database is only half a
backup. See DEPLOY-PROD.md.
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> Path:
    db_path = Path(os.environ.get("OVERLAP_DB_PATH", "overlap.db"))
    backup_dir = Path(os.environ.get("OVERLAP_BACKUP_DIR", "backups"))
    if not db_path.exists():
        print(f"error: database not found at {db_path}", file=sys.stderr)
        raise SystemExit(1)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"overlap-{stamp}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            with out:
                src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    print(dest)
    return dest


if __name__ == "__main__":
    main()
