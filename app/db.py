import os
import sqlite3
from contextlib import closing
from pathlib import Path

from app.audit import WRITE_LOCK, utc_now

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
    raw_payload TEXT NOT NULL,
    raw_truncated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS light_restore_records (
    room TEXT NOT NULL,
    light_id TEXT NOT NULL,
    was_off INTEGER NOT NULL CHECK(was_off IN (0, 1)),
    restore_percent INTEGER,
    dim_percent INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (room, light_id)
);
CREATE TABLE IF NOT EXISTS automation_room_owners (
    target_room TEXT PRIMARY KEY,
    trigger_room TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at);
-- Replaced by idx_events_labels_cover: plain columns let the counter re-seed read
-- only the index, where the old expression index still looked up every row.
DROP INDEX IF EXISTS idx_events_counts_cover;
CREATE INDEX IF NOT EXISTS idx_events_labels_cover ON events(event, player_title, account_title);
CREATE INDEX IF NOT EXISTS idx_events_clients_cover
    ON events(player_title, player_uuid, received_at)
    WHERE player_title IS NOT NULL;
"""


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # Databases created before the stored-size cap lack the marker column.
    if "raw_truncated" not in {row[1] for row in conn.execute("PRAGMA table_info(events)")}:
        conn.execute("ALTER TABLE events ADD COLUMN raw_truncated INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    return conn


def insert_event(conn, received_at, event, payload, raw_payload_json, raw_truncated=False):
    with WRITE_LOCK:
        metadata = (payload or {}).get("Metadata") or {}
        account = (payload or {}).get("Account") or {}
        player = (payload or {}).get("Player") or {}

        cursor = conn.execute(
            """
            INSERT INTO events (
                received_at, event, account_title, player_title, player_uuid,
                media_type, title, grandparent_title, rating_key, raw_payload, raw_truncated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                1 if raw_truncated else 0,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def last_received_at(conn):
    row = conn.execute("SELECT MAX(received_at) FROM events").fetchone()
    return row[0] if row else None


def labeled_event_counts(conn, label):
    """Event totals keyed by label(event, player, account), with missing values as 'unknown'.

    Rows stream from the covering index and are folded as they arrive, so memory
    is bounded by the label space, not by the number of distinct stored values.
    """
    totals = {}
    rows = conn.execute("SELECT event, player_title, account_title, COUNT(*) FROM events GROUP BY 1, 2, 3")
    for *values, count in rows:
        key = label(*("unknown" if value in (None, "") else value for value in values))
        totals[key] = totals.get(key, 0) + count
    return totals


def event_counts(conn):
    """Event totals per (event, player, account), labeled the way /webhook reads the payload."""
    return [(*key, count) for key, count in labeled_event_counts(conn, lambda *values: values).items()]


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


# --- light restore records ----------------------------------------------------
# One row per (room, light) between a dim and its restore. Each call owns its
# connection so the light-action worker never shares the request connection.


def _restore_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.executescript(SCHEMA)
    return conn


def get_restore_record(room, light_id):
    with closing(_restore_conn()) as conn:
        row = conn.execute(
            "SELECT was_off, restore_percent, dim_percent FROM light_restore_records WHERE room = ? AND light_id = ?",
            (room, light_id),
        ).fetchone()
    return {"was_off": bool(row[0]), "restore_percent": row[1], "dim_percent": row[2]} if row else None


def put_restore_record(room, light_id, was_off, restore_percent, dim_percent):
    with WRITE_LOCK, closing(_restore_conn()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO light_restore_records VALUES (?, ?, ?, ?, ?, ?)",
            (room, light_id, 1 if was_off else 0, restore_percent, dim_percent, utc_now()),
        )


def delete_restore_record(room, light_id):
    with WRITE_LOCK, closing(_restore_conn()) as conn, conn:
        conn.execute("DELETE FROM light_restore_records WHERE room = ? AND light_id = ?", (room, light_id))


def set_automation_owner(target_room, trigger_room):
    """The latest dim owns the target's pending restore; own playback clears ownership."""
    with WRITE_LOCK, closing(_restore_conn()) as conn, conn:
        if trigger_room is None:
            conn.execute("DELETE FROM automation_room_owners WHERE target_room = ?", (target_room,))
        else:
            conn.execute("INSERT OR REPLACE INTO automation_room_owners VALUES (?, ?)",
                         (target_room, trigger_room))


def automation_targets(trigger_room):
    with closing(_restore_conn()) as conn:
        return {row[0] for row in conn.execute(
            "SELECT target_room FROM automation_room_owners WHERE trigger_room = ?", (trigger_room,))}


def automation_sources():
    with closing(_restore_conn()) as conn:
        return {row[0] for row in conn.execute("SELECT DISTINCT trigger_room FROM automation_room_owners")}


def rooms_with_restore_records():
    with closing(_restore_conn()) as conn:
        return {row[0] for row in conn.execute("SELECT DISTINCT room FROM light_restore_records")}
