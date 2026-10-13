"""SQLite persistence — the single source of truth for users, listings,
evaluations, notifications, search runs and processed Gmail messages.
Plain stdlib sqlite3, no ORM.

Schema changes stay backward-compatible (new tables, new nullable columns), so
an older release still runs against a newer database file.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from offer_finder.core.models import (
    Evaluation,
    Listing,
    ListingStatus,
    SearchSpec,
    User,
    UserStatus,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_specs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    query TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('search', 'gmail')),
    status TEXT NOT NULL CHECK (status IN ('discovered', 'fetched', 'out_of_range', 'fetch_failed')),
    search_spec_id INTEGER REFERENCES search_specs(id),
    description TEXT,
    price_raw TEXT,
    price_eur INTEGER,
    location_text TEXT,
    plz TEXT,
    posted_at_raw TEXT,
    image_urls TEXT NOT NULL DEFAULT '[]',
    attributes TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listings(listing_id),
    search_spec_id INTEGER REFERENCES search_specs(id),
    score INTEGER NOT NULL,
    recommended INTEGER NOT NULL,
    product_verdict TEXT NOT NULL,
    deal_verdict TEXT NOT NULL,
    input_json TEXT NOT NULL,
    judgment_json TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evaluations_slot
    ON evaluations (listing_id, IFNULL(search_spec_id, 0));

CREATE TABLE IF NOT EXISTS notifications (
    listing_id TEXT NOT NULL REFERENCES listings(listing_id),
    chat_id INTEGER NOT NULL,
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
    sent_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, chat_id)
);

CREATE TABLE IF NOT EXISTS gmail_messages (
    message_id TEXT PRIMARY KEY,
    subject TEXT,
    listing_ids TEXT NOT NULL,
    search_spec_id INTEGER REFERENCES search_specs(id),
    error TEXT,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'blocked')),
    is_admin INTEGER NOT NULL DEFAULT 0,
    home_plz TEXT,
    radius_km INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    search_spec_id INTEGER REFERENCES search_specs(id),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'interrupted')),
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_runs_chat ON search_runs (chat_id, created_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


# --- search specs ----------------------------------------------------------------


def insert_search_spec(conn: sqlite3.Connection, chat_id: int, spec: SearchSpec) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO search_specs (chat_id, query, spec_json, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, spec.query, spec.model_dump_json(), _now()),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def get_search_spec(conn: sqlite3.Connection, search_spec_id: int) -> SearchSpec | None:
    row = conn.execute("SELECT spec_json FROM search_specs WHERE id = ?", (search_spec_id,)).fetchone()
    return SearchSpec.model_validate_json(row["spec_json"]) if row else None


def active_search_specs(
    conn: sqlite3.Connection, owner_ids: list[int] | None = None
) -> list[tuple[int, SearchSpec]]:
    """Newest first; owner_ids limits them to specs of those chats."""
    sql = "SELECT id, spec_json FROM search_specs WHERE active = 1"
    params: list[Any] = []
    if owner_ids is not None:
        sql += f" AND chat_id IN ({', '.join('?' * len(owner_ids))})"
        params.extend(owner_ids)
    rows = conn.execute(sql + " ORDER BY id DESC", params)
    return [(row["id"], SearchSpec.model_validate_json(row["spec_json"])) for row in rows]


# --- listings --------------------------------------------------------------------

_LISTING_FIELDS = (
    "url", "title", "source", "status", "search_spec_id", "description", "price_raw",
    "price_eur", "location_text", "plz", "posted_at_raw", "image_urls", "attributes",
)


def _listing_values(listing: Listing) -> list[Any]:
    data = listing.model_dump()
    data["image_urls"] = json.dumps(listing.image_urls)
    data["attributes"] = json.dumps(listing.attributes, ensure_ascii=False)
    return [data[field] for field in _LISTING_FIELDS]


def _row_to_listing(row: sqlite3.Row) -> Listing:
    data = {key: row[key] for key in ("listing_id", *_LISTING_FIELDS)}
    data["image_urls"] = json.loads(row["image_urls"])
    data["attributes"] = json.loads(row["attributes"])
    return Listing(**data)


def insert_discovered(conn: sqlite3.Connection, listing: Listing) -> bool:
    """Store a listing the first time any path sees it. Returns False (and
    changes nothing) if it is already known."""
    now = _now()
    columns = ", ".join(("listing_id", *_LISTING_FIELDS, "first_seen_at", "updated_at"))
    placeholders = ", ".join("?" * (len(_LISTING_FIELDS) + 3))
    with conn:
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO listings ({columns}) VALUES ({placeholders})",
            [listing.listing_id, *_listing_values(listing), now, now],
        )
    return cursor.rowcount == 1


def update_listing(conn: sqlite3.Connection, listing: Listing) -> None:
    """Overwrite scraped fields (e.g. after a detail fetch). Keeps source and
    first_seen_at from whichever path discovered the listing first."""
    fields = [f for f in _LISTING_FIELDS if f not in ("source", "search_spec_id")]
    data = dict(zip(_LISTING_FIELDS, _listing_values(listing), strict=True))
    assignments = ", ".join(f"{field} = ?" for field in fields)
    with conn:
        conn.execute(
            f"UPDATE listings SET {assignments}, updated_at = ? WHERE listing_id = ?",
            [*(data[f] for f in fields), _now(), listing.listing_id],
        )


def set_listing_status(conn: sqlite3.Connection, listing_id: str, status: ListingStatus) -> None:
    with conn:
        conn.execute(
            "UPDATE listings SET status = ?, updated_at = ? WHERE listing_id = ?",
            (status, _now(), listing_id),
        )


def get_listing(conn: sqlite3.Connection, listing_id: str) -> Listing | None:
    row = conn.execute("SELECT * FROM listings WHERE listing_id = ?", (listing_id,)).fetchone()
    return _row_to_listing(row) if row else None


def listings_pending_evaluation(
    conn: sqlite3.Connection, search_spec_id: int | None, listing_ids: list[str] | None = None
) -> list[Listing]:
    """Fetched listings with no evaluation yet for this spec (None = no spec)."""
    sql = (
        "SELECT * FROM listings l WHERE l.status = 'fetched' AND NOT EXISTS ("
        " SELECT 1 FROM evaluations e WHERE e.listing_id = l.listing_id AND e.search_spec_id IS ?)"
    )
    params: list[Any] = [search_spec_id]
    if listing_ids is not None:
        sql += f" AND l.listing_id IN ({', '.join('?' * len(listing_ids))})"
        params.extend(listing_ids)
    rows = conn.execute(sql + " ORDER BY l.first_seen_at, l.listing_id", params)
    return [_row_to_listing(row) for row in rows]


# --- evaluations -----------------------------------------------------------------


def save_evaluation(conn: sqlite3.Connection, evaluation: Evaluation) -> int:
    input_json = json.dumps(
        {
            "facts": evaluation.facts.model_dump(),
            "checks": evaluation.checks.model_dump(),
            "user_prompt": evaluation.user_prompt,
        },
        ensure_ascii=False,
    )
    judgment = evaluation.judgment
    with conn:
        cursor = conn.execute(
            "INSERT INTO evaluations (listing_id, search_spec_id, score, recommended, product_verdict,"
            " deal_verdict, input_json, judgment_json, raw_response, model, prompt_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evaluation.listing_id,
                evaluation.search_spec_id,
                judgment.score,
                int(evaluation.recommended),
                judgment.product_assessment.verdict,
                judgment.deal_assessment.verdict,
                input_json,
                judgment.model_dump_json(),
                json.dumps(evaluation.raw_response, ensure_ascii=False),
                evaluation.model,
                evaluation.prompt_version,
                _now(),
            ),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _row_to_evaluation(row: sqlite3.Row) -> Evaluation:
    stored_input = json.loads(row["input_json"])
    return Evaluation(
        listing_id=row["listing_id"],
        search_spec_id=row["search_spec_id"],
        facts=stored_input["facts"],
        checks=stored_input["checks"],
        judgment=json.loads(row["judgment_json"]),
        recommended=bool(row["recommended"]),
        model=row["model"],
        prompt_version=row["prompt_version"],
        user_prompt=stored_input["user_prompt"],
        raw_response=json.loads(row["raw_response"]),
    )


def evaluations_for_spec(conn: sqlite3.Connection, search_spec_id: int) -> list[tuple[Listing, Evaluation]]:
    rows = conn.execute(
        "SELECT * FROM evaluations WHERE search_spec_id = ? ORDER BY score DESC, id", (search_spec_id,)
    ).fetchall()
    result = []
    for row in rows:
        listing = get_listing(conn, row["listing_id"])
        assert listing is not None
        result.append((listing, _row_to_evaluation(row)))
    return result


# --- notifications ---------------------------------------------------------------


# Who may see an evaluation: the owner of its search, and admins for alert-email
# evaluations (no spec, or a spec of an admin). Nobody sees a friend's search.
# Uses the named parameters :chat and :is_admin.
_VISIBLE = """(
    EXISTS (SELECT 1 FROM search_specs s WHERE s.id = {e}.search_spec_id AND s.chat_id = :chat)
    OR (:is_admin AND ({e}.search_spec_id IS NULL OR EXISTS (
        SELECT 1 FROM search_specs s JOIN users u ON u.telegram_id = s.chat_id
        WHERE s.id = {e}.search_spec_id AND u.is_admin = 1)))
)"""


def evaluation_visible_to(conn: sqlite3.Connection, evaluation_id: int, chat_id: int, is_admin: bool) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM evaluations e WHERE e.id = :id AND {_VISIBLE.format(e='e')}",
        {"id": evaluation_id, "chat": chat_id, "is_admin": is_admin},
    ).fetchone()
    return row is not None


def pending_notifications(
    conn: sqlite3.Connection, chat_id: int, search_spec_id: int | None = None, *, is_admin: bool
) -> list[tuple[Listing, Evaluation, int]]:
    """Recommended listings this chat may see and hasn't been sent yet, best
    first. Returns one row per listing even if several specs recommended it."""
    rows = conn.execute(
        f"""
        SELECT e.*, e.id AS evaluation_id FROM evaluations e
        WHERE e.recommended = 1
          AND (:spec IS NULL OR e.search_spec_id = :spec)
          AND {_VISIBLE.format(e="e")}
          AND NOT EXISTS (
              SELECT 1 FROM notifications n WHERE n.listing_id = e.listing_id AND n.chat_id = :chat)
          AND e.id = (
              SELECT MIN(e2.id) FROM evaluations e2
              WHERE e2.listing_id = e.listing_id AND e2.recommended = 1
                AND (:spec IS NULL OR e2.search_spec_id = :spec)
                AND {_VISIBLE.format(e="e2")})
        ORDER BY e.score DESC, e.id
        """,
        {"spec": search_spec_id, "chat": chat_id, "is_admin": is_admin},
    ).fetchall()
    result = []
    for row in rows:
        listing = get_listing(conn, row["listing_id"])
        assert listing is not None  # enforced by the foreign key
        result.append((listing, _row_to_evaluation(row), row["evaluation_id"]))
    return result


def picks_for_spec(
    conn: sqlite3.Connection, chat_id: int, search_spec_id: int
) -> list[tuple[Listing, Evaluation, int, bool]]:
    """Recommended listings of one search, best first: (listing, evaluation,
    evaluation id, already sent to this chat before). Empty unless the chat
    owns the search."""
    rows = conn.execute(
        """
        SELECT e.*, e.id AS evaluation_id, EXISTS (
            SELECT 1 FROM notifications n WHERE n.listing_id = e.listing_id AND n.chat_id = :chat) AS seen
        FROM evaluations e
        WHERE e.recommended = 1 AND e.search_spec_id = :spec
          AND EXISTS (SELECT 1 FROM search_specs s WHERE s.id = :spec AND s.chat_id = :chat)
        ORDER BY e.score DESC, e.id
        """,
        {"spec": search_spec_id, "chat": chat_id},
    ).fetchall()
    result = []
    for row in rows:
        listing = get_listing(conn, row["listing_id"])
        assert listing is not None  # enforced by the foreign key
        result.append((listing, _row_to_evaluation(row), row["evaluation_id"], bool(row["seen"])))
    return result


def get_evaluation(conn: sqlite3.Connection, evaluation_id: int) -> tuple[Listing, Evaluation] | None:
    row = conn.execute("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)).fetchone()
    if row is None:
        return None
    listing = get_listing(conn, row["listing_id"])
    assert listing is not None
    return listing, _row_to_evaluation(row)


def record_notification(conn: sqlite3.Connection, listing_id: str, chat_id: int, evaluation_id: int) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO notifications (listing_id, chat_id, evaluation_id, sent_at)"
            " VALUES (?, ?, ?, ?)",
            (listing_id, chat_id, evaluation_id, _now()),
        )


# --- gmail messages --------------------------------------------------------------


def gmail_message_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM gmail_messages WHERE message_id = ?", (message_id,)).fetchone()
    return row is not None


def record_gmail_message(
    conn: sqlite3.Connection,
    message_id: str,
    subject: str | None,
    listing_ids: list[str],
    search_spec_id: int | None,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO gmail_messages"
            " (message_id, subject, listing_ids, search_spec_id, error, processed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, subject, json.dumps(listing_ids), search_spec_id, error, _now()),
        )


# --- users -----------------------------------------------------------------------


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        name=row["name"],
        status=row["status"],
        is_admin=bool(row["is_admin"]),
        home_plz=row["home_plz"],
        radius_km=row["radius_km"],
    )


def sync_admins(conn: sqlite3.Connection, admin_ids: list[int]) -> None:
    """TELEGRAM_ALLOWLIST is the list of admins: make them approved admins and
    take admin rights from anyone no longer on it."""
    now = _now()
    placeholders = ", ".join("?" * len(admin_ids))
    with conn:
        for telegram_id in admin_ids:
            conn.execute(
                "INSERT INTO users (telegram_id, name, status, is_admin, created_at, updated_at)"
                " VALUES (?, 'admin', 'approved', 1, ?, ?)"
                " ON CONFLICT (telegram_id) DO UPDATE SET is_admin = 1, status = 'approved'"
                " WHERE is_admin = 0 OR status != 'approved'",
                (telegram_id, now, now),
            )
        conn.execute(
            f"UPDATE users SET is_admin = 0 WHERE is_admin = 1 AND telegram_id NOT IN ({placeholders})",
            admin_ids,
        )


def get_user(conn: sqlite3.Connection, telegram_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    return _row_to_user(row) if row else None


def request_access(conn: sqlite3.Connection, telegram_id: int, name: str) -> tuple[User, bool]:
    """A stranger wrote to the bot: store them as pending (once). Returns the
    user and whether this was the first request."""
    now = _now()
    with conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'pending', ?, ?)",
            (telegram_id, name, now, now),
        )
    user = get_user(conn, telegram_id)
    assert user is not None
    return user, cursor.rowcount == 1


def set_user_status(conn: sqlite3.Connection, telegram_id: int, status: UserStatus) -> bool:
    """Returns False if the user doesn't exist or already had that status."""
    with conn:
        cursor = conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE telegram_id = ? AND status != ?",
            (status, _now(), telegram_id, status),
        )
    return cursor.rowcount == 1


def set_user_home(conn: sqlite3.Connection, telegram_id: int, home_plz: str | None) -> None:
    with conn:
        conn.execute(
            "UPDATE users SET home_plz = ?, updated_at = ? WHERE telegram_id = ?", (home_plz, _now(), telegram_id)
        )


def set_user_radius(conn: sqlite3.Connection, telegram_id: int, radius_km: int | None) -> None:
    """None = the .env default, 0 = any distance."""
    with conn:
        conn.execute(
            "UPDATE users SET radius_km = ?, updated_at = ? WHERE telegram_id = ?", (radius_km, _now(), telegram_id)
        )


def list_users(conn: sqlite3.Connection) -> list[User]:
    rows = conn.execute("SELECT * FROM users ORDER BY is_admin DESC, created_at, telegram_id")
    return [_row_to_user(row) for row in rows]


# --- search runs -----------------------------------------------------------------

RunRefusal = Literal["busy", "limit"]
RunStatus = Literal["done", "failed"]


def reserve_search_run(
    conn: sqlite3.Connection, chat_id: int, daily_limit: int | None, now: datetime | None = None
) -> int | RunRefusal:
    """Count a confirmed search before it starts. Refuses ("busy") while this
    chat has a search queued or running, and ("limit") once it started
    daily_limit searches in the last 24 hours (None = no limit). Check and
    insert happen in one write transaction, so two taps can't both pass."""
    now = now or datetime.now(UTC)
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        busy = conn.execute(
            "SELECT 1 FROM search_runs WHERE chat_id = ? AND status IN ('queued', 'running')", (chat_id,)
        ).fetchone()
        if busy:
            conn.rollback()
            return "busy"
        if daily_limit is not None:
            (recent,) = conn.execute(
                "SELECT COUNT(*) FROM search_runs WHERE chat_id = ? AND created_at >= ?", (chat_id, since)
            ).fetchone()
            if recent >= daily_limit:
                conn.rollback()
                return "limit"
        cursor = conn.execute(
            "INSERT INTO search_runs (chat_id, status, created_at) VALUES (?, 'queued', ?)",
            (chat_id, now.isoformat(timespec="seconds")),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def start_search_run(conn: sqlite3.Connection, run_id: int) -> None:
    with conn:
        conn.execute("UPDATE search_runs SET status = 'running' WHERE id = ?", (run_id,))


def finish_search_run(
    conn: sqlite3.Connection, run_id: int, status: RunStatus, search_spec_id: int | None
) -> None:
    with conn:
        conn.execute(
            "UPDATE search_runs SET status = ?, search_spec_id = ?, finished_at = ? WHERE id = ?",
            (status, search_spec_id, _now(), run_id),
        )


def interrupt_unfinished_runs(conn: sqlite3.Connection) -> int:
    """At startup: searches cut off by a restart no longer block their user
    (they still count towards the daily limit)."""
    with conn:
        cursor = conn.execute(
            "UPDATE search_runs SET status = 'interrupted', finished_at = ?"
            " WHERE status IN ('queued', 'running')",
            (_now(),),
        )
    return cursor.rowcount


# --- inspection ------------------------------------------------------------------


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    by_status = conn.execute("SELECT status, COUNT(*) FROM listings GROUP BY status ORDER BY status")
    return {
        "search_specs": count("SELECT COUNT(*) FROM search_specs"),
        "listings": count("SELECT COUNT(*) FROM listings"),
        "listings_by_status": {row[0]: row[1] for row in by_status},
        "evaluations": count("SELECT COUNT(*) FROM evaluations"),
        "recommended": count("SELECT COUNT(*) FROM evaluations WHERE recommended = 1"),
        "notifications": count("SELECT COUNT(*) FROM notifications"),
        "gmail_messages": count("SELECT COUNT(*) FROM gmail_messages"),
    }


def export_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per evaluation, with an empty my_label column for hand-labelling."""
    rows = conn.execute(
        """
        SELECT e.id AS evaluation_id, e.listing_id, e.search_spec_id, l.title, l.price_eur, l.url,
               e.score, e.recommended, e.product_verdict, e.deal_verdict, e.model,
               e.prompt_version, e.created_at
        FROM evaluations e JOIN listings l ON l.listing_id = e.listing_id
        ORDER BY e.id
        """
    )
    return [{**dict(row), "my_label": ""} for row in rows]
