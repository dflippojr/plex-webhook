import sqlite3
from pathlib import Path

DB_PATH = Path("/data/plex_events.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    event TEXT,
    account_title TEXT,
    player_title TEXT,
    player_uuid TEXT,
    media_type TEXT,
    title TEXT,
    grandparent_title TEXT,
    rating_key TEXT,
    raw_payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at);
"""


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def insert_event(conn, received_at, event, payload, raw_payload_json):
    metadata = (payload or {}).get("Metadata") or {}
    account = (payload or {}).get("Account") or {}
    player = (payload or {}).get("Player") or {}

    conn.execute(
        """
        INSERT INTO events (
            received_at, event, account_title, player_title, player_uuid,
            media_type, title, grandparent_title, rating_key, raw_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            received_at,
            event,
            account.get("title"),
            player.get("title"),
            player.get("uuid"),
            metadata.get("type"),
            metadata.get("title"),
            metadata.get("grandparentTitle"),
            metadata.get("ratingKey"),
            raw_payload_json,
        ),
    )
    conn.commit()
