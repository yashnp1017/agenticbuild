"""SQLite storage for raw Gmail messages.

Deliberately dumb: this layer stores what Gmail gave us, unmodified.
Normalisation and extraction happen downstream so they can be re-run
without re-fetching anything.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "mail.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,      -- Gmail message id
    thread_id       TEXT NOT NULL,
    history_id      INTEGER,
    internal_date   INTEGER,               -- epoch millis, Gmail's own timestamp
    from_name       TEXT,
    from_email      TEXT,
    to_emails       TEXT,                  -- JSON array
    cc_emails       TEXT,                  -- JSON array
    subject         TEXT,
    snippet         TEXT,
    label_ids       TEXT,                  -- JSON array
    body_text       TEXT,
    body_html       TEXT,
    raw_payload     TEXT,                  -- full API response, for reprocessing
    fetched_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_date   ON messages(internal_date DESC);
CREATE INDEX IF NOT EXISTS idx_messages_from   ON messages(from_email);

-- Key/value store for sync bookkeeping (last historyId, last full sync time).
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safer concurrent reads while ingesting
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_message(conn: sqlite3.Connection, msg: dict) -> None:
    """Insert or overwrite a parsed message row."""
    conn.execute(
        """
        INSERT OR REPLACE INTO messages
            (id, thread_id, history_id, internal_date, from_name, from_email,
             to_emails, cc_emails, subject, snippet, label_ids,
             body_text, body_html, raw_payload)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            msg["id"],
            msg["thread_id"],
            msg["history_id"],
            msg["internal_date"],
            msg["from_name"],
            msg["from_email"],
            json.dumps(msg["to_emails"]),
            json.dumps(msg["cc_emails"]),
            msg["subject"],
            msg["snippet"],
            json.dumps(msg["label_ids"]),
            msg["body_text"],
            msg["body_html"],
            json.dumps(msg["raw_payload"]),
        ),
    )


def existing_ids(conn: sqlite3.Connection) -> set:
    """Message ids already stored, so we can skip re-fetching them."""
    return {row[0] for row in conn.execute("SELECT id FROM messages")}


def get_state(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """,
        (key, str(value)),
    )
    conn.commit()
