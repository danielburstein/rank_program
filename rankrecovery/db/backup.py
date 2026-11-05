"""Backup and restore.

SQLite: uses the online backup API (safe while the app runs). Other engines:
documented to use the server's native tools (pg_dump) and export CSV of
derived results through the API.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from rankrecovery.db.engine import Database


def backup_sqlite(db: Database, target_dir: str | Path, *, label: str = "") -> Path:
    if not db.is_sqlite:
        raise RuntimeError("Online backup is only implemented for SQLite; use pg_dump for PostgreSQL")
    source_path = Database._sqlite_path(db.url)
    if source_path == ":memory:":
        raise RuntimeError("Cannot back up an in-memory database")
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"rankrecovery-{stamp}{('-' + label) if label else ''}.db"
    target = target_dir / name
    with db.write_lock:
        src = sqlite3.connect(source_path)
        try:
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    return target


def restore_sqlite(db: Database, backup_file: str | Path) -> Path:
    """Replace the live database with ``backup_file``. Caller must stop workers first."""
    if not db.is_sqlite:
        raise RuntimeError("Restore is only implemented for SQLite")
    source_path = Path(Database._sqlite_path(db.url))
    backup_file = Path(backup_file)
    if not backup_file.exists():
        raise FileNotFoundError(backup_file)
    # Validate the backup opens as SQLite before touching the live file.
    conn = sqlite3.connect(str(backup_file))
    try:
        conn.execute("PRAGMA schema_version").fetchone()
    finally:
        conn.close()
    with db.write_lock:
        db.engine.dispose()
        safety = source_path.with_suffix(source_path.suffix + ".pre-restore")
        if source_path.exists():
            shutil.copy2(source_path, safety)
        for suffix in ("-wal", "-shm"):
            side = Path(str(source_path) + suffix)
            if side.exists():
                side.unlink()
        shutil.copy2(backup_file, source_path)
    return source_path
