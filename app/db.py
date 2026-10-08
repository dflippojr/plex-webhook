import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", "/data/plex_events.db"))

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
CREATE INDEX IF NOT EXISTS idx_events_counts_cover ON events(
    COALESCE(NULLIF(event, ''), 'unknown'),
    COALESCE(NULLIF(player_title, ''), 'unknown'),
    COALESCE(NULLIF(account_title, ''), 'unknown')
);
CREATE INDEX IF NOT EXISTS idx_events_clients_cover
    ON events(player_title, player_uuid, received_at)
    WHERE player_title IS NOT NULL;
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


def last_received_at(conn):
    row = conn.execute("SELECT MAX(received_at) FROM events").fetchone()
    return row[0] if row else None


def event_counts(conn):
    """Event totals per (event, player, account), labeled the way /webhook labels them."""
    return conn.execute(
        """
        SELECT COALESCE(NULLIF(event, ''), 'unknown'),
               COALESCE(NULLIF(player_title, ''), 'unknown'),
               COALESCE(NULLIF(account_title, ''), 'unknown'),
               COUNT(*)
        FROM events
        GROUP BY 1, 2, 3
        """
    ).fetchall()


def list_known_clients(conn):
    rows = conn.execute(
        """
        SELECT player_title, player_uuid, COUNT(*) as event_count, MAX(received_at) as last_seen
        FROM events
        WHERE player_title IS NOT NULL
        GROUP BY player_title, player_uuid
        ORDER BY last_seen DESC
        """
    ).fetchall()
    return [
        {"title": row[0], "uuid": row[1], "event_count": row[2], "last_seen": row[3]}
        for row in rows
    ]
