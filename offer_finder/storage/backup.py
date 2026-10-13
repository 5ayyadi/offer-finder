"""SQLite backups and integrity checks. backup_db() uses SQLite's online
backup API, so it is safe while the bot is writing (WAL mode); it only ever
contains committed data. verify_db() is what to run before trusting or
restoring a backup.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_PREFIX = "offer_finder-"
_REQUIRED_TABLES = ("search_specs", "listings", "evaluations", "notifications", "gmail_messages")


def backup_db(db_path: str, backup_dir: str, keep: int, now: datetime | None = None) -> Path:
    """Write offer_finder-<UTC timestamp>.db to backup_dir and delete all but
    the newest `keep` of those files (nothing else in the folder)."""
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    folder = Path(backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    target = folder / f"{_PREFIX}{stamp}.db"
    partial = folder / f".{target.name}.partial"  # never looks like a finished backup
    partial.unlink(missing_ok=True)

    source = sqlite3.connect(db_path, timeout=10.0)  # only read; a read-only open can fail on WAL files
    dest = sqlite3.connect(partial)
    try:
        source.backup(dest)
        dest.execute("PRAGMA journal_mode = DELETE")  # a single self-contained file
    finally:
        dest.close()
        source.close()
    partial.replace(target)

    backups = sorted(folder.glob(f"{_PREFIX}*.db"))
    for old in backups[: max(len(backups) - keep, 0)]:
        old.unlink()
    return target


def verify_db(path: str) -> list[str]:
    """Problems found in the database file; an empty list means it's fine."""
    if not Path(path).is_file():
        return [f"{path}: file not found"]
    try:
        conn = sqlite3.connect(path)  # existence checked above; the checks below only read
        try:
            results = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return [f"{path}: not a readable SQLite database ({exc})"]
    problems = [f"{path}: {result}" for result in results if result != "ok"]
    problems += [f"{path}: table {table} is missing" for table in _REQUIRED_TABLES if table not in tables]
    return problems
