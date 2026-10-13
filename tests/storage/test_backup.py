"""Online SQLite backups (the backup API, safe while the bot writes) and the
integrity check used before trusting or restoring one."""

import sqlite3
from datetime import UTC, datetime, timedelta

from offer_finder.storage import db
from offer_finder.storage.backup import backup_db, verify_db
from tests.helpers.factories import make_listing


def _db_with_listing(tmp_path, listing_id="1"):
    path = str(tmp_path / "live.db")
    conn = db.connect(path)
    db.init_db(conn)
    db.insert_discovered(conn, make_listing(listing_id))
    return path, conn


def test_backup_is_a_verified_copy(tmp_path):
    path, conn = _db_with_listing(tmp_path)
    conn.close()
    backup = backup_db(path, str(tmp_path / "backups"), keep=14)
    assert backup.name.startswith("offer_finder-") and backup.suffix == ".db"
    assert verify_db(str(backup)) == []
    copy = db.connect(str(backup))
    assert db.get_listing(copy, "1") is not None


def test_backup_while_a_write_is_in_progress_holds_only_committed_data(tmp_path):
    path, conn = _db_with_listing(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE listings SET title = 'uncommitted' WHERE listing_id = '1'")
    backup = backup_db(path, str(tmp_path / "backups"), keep=14)
    conn.rollback()
    conn.close()
    copy = db.connect(str(backup))
    assert db.get_listing(copy, "1").title != "uncommitted"  # type: ignore[union-attr]


def test_only_the_newest_backups_are_kept(tmp_path):
    path, conn = _db_with_listing(tmp_path)
    conn.close()
    start = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
    made = [backup_db(path, str(tmp_path / "backups"), keep=2, now=start + timedelta(days=i)) for i in range(4)]
    remaining = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert remaining == sorted(p.name for p in made[-2:])


def test_backup_never_touches_other_files_in_the_folder(tmp_path):
    path, conn = _db_with_listing(tmp_path)
    conn.close()
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "notes.txt").write_text("keep me")
    for i in range(3):
        backup_db(path, str(folder), keep=1, now=datetime(2026, 10, 1 + i, tzinfo=UTC))
    assert (folder / "notes.txt").exists()


def test_verify_db_reports_a_missing_or_broken_file(tmp_path):
    assert verify_db(str(tmp_path / "nope.db"))
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not sqlite" * 100)
    assert verify_db(str(broken))


def test_verify_db_reports_a_database_without_the_app_tables(tmp_path):
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x (y)").connection.commit()
    problems = verify_db(str(other))
    assert any("listings" in problem for problem in problems)
