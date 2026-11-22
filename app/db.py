import json
from contextlib import closing
from pathlib import Path

from app import storage
from app.audit import WRITE_LOCK, utc_now

# The events and restore-record tables as one script; app.storage owns the DDL and its migrations.
SCHEMA = storage.EVENTS_SCHEMA


# Payload values insert_event stores, as (group, field names), in the order of their
# events columns below. main's payload validation guards every value listed here.
STORED_FIELDS = (
    ("Account", ("title",)),
    ("Player", ("title", "uuid")),
    ("Metadata", ("type", "title", "grandparentTitle", "ratingKey")),
)


def stored_values(payload):
    """The STORED_FIELDS values of a payload, in table order; missing groups and fields are None."""
    for group, names in STORED_FIELDS:
        fields = (payload or {}).get(group) or {}
        for name in names:
            yield fields.get(name)


def insert_event(conn, received_at, event, payload, raw_payload_json, raw_truncated=False, attachments=None):
    """Store one event. attachments is the list of file-part metadata, saved as JSON text ("[]" for none)."""
    # ASCII-escaped JSON, so an unpaired surrogate in an unauthenticated filename cannot fail the insert.
    attachments_json = None if attachments is None else json.dumps(attachments)
    with WRITE_LOCK:
        cursor = conn.execute(
            """
            INSERT INTO events (
                received_at, event, account_title, player_title, player_uuid,
                media_type, title, grandparent_title, rating_key, raw_payload, raw_truncated, attachments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (received_at, event, *stored_values(payload), raw_payload_json, 1 if raw_truncated else 0,
             attachments_json),
        )
        conn.commit()
        return cursor.lastrowid


def last_received_at(conn):
    row = conn.execute("SELECT MAX(received_at) FROM events").fetchone()
    return row[0] if row else None


def labeled_event_counts(conn, label):
    """Event totals keyed by label(event, player_title, player_uuid, account), with missing values as 'unknown'.

    Rows stream from the covering index and are folded as they arrive, so memory
    is bounded by the label space, not by the number of distinct stored values.
    """
    totals = {}
    rows = conn.execute(
        "SELECT event, player_title, player_uuid, account_title, COUNT(*) FROM events GROUP BY 1, 2, 3, 4"
    )
    for *values, count in rows:
        key = label(*("unknown" if value in (None, "") else value for value in values))
        totals[key] = totals.get(key, 0) + count
    return totals


def event_counts(conn):
    """Event totals per (event, player, account), labeled the way /webhook reads the payload."""
    totals = labeled_event_counts(conn, lambda event, player, _uuid, account: (event, player, account))
    return [(*key, count) for key, count in totals.items()]


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
# connection so the light-action worker never shares the request connection. The
# tables exist already: service startup migrated the file (app.storage.open_database).


class RestoreRecords:
    """Restore records and automation owners in the service's database file."""

    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        return storage.connect(self.path)

    def get_restore_record(self, room, light_id):
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT was_off, restore_percent, dim_percent FROM light_restore_records WHERE room = ? AND light_id = ?",
                (room, light_id),
            ).fetchone()
        return {"was_off": bool(row[0]), "restore_percent": row[1], "dim_percent": row[2]} if row else None

    def put_restore_record(self, room, light_id, was_off, restore_percent, dim_percent):
        with WRITE_LOCK, closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO light_restore_records VALUES (?, ?, ?, ?, ?, ?)",
                (room, light_id, 1 if was_off else 0, restore_percent, dim_percent, utc_now()),
            )

    def delete_restore_record(self, room, light_id):
        with WRITE_LOCK, closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM light_restore_records WHERE room = ? AND light_id = ?", (room, light_id))

    def set_automation_owner(self, target_room, trigger_room):
        """The latest dim owns the target's pending restore; own playback clears ownership."""
        with WRITE_LOCK, closing(self._connect()) as conn, conn:
            if trigger_room is None:
                conn.execute("DELETE FROM automation_room_owners WHERE target_room = ?", (target_room,))
            else:
                conn.execute("INSERT OR REPLACE INTO automation_room_owners VALUES (?, ?)",
                             (target_room, trigger_room))

    def automation_owner(self, target_room):
        """The trigger room that owns the target's pending automatic restore, or None."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT trigger_room FROM automation_room_owners WHERE target_room = ?",
                               (target_room,)).fetchone()
        return row[0] if row else None

    def automation_targets(self, trigger_room):
        with closing(self._connect()) as conn:
            return {row[0] for row in conn.execute(
                "SELECT target_room FROM automation_room_owners WHERE trigger_room = ?", (trigger_room,))}

    def automation_sources(self):
        with closing(self._connect()) as conn:
            return {row[0] for row in conn.execute("SELECT DISTINCT trigger_room FROM automation_room_owners")}

    def rooms_with_restore_records(self):
        with closing(self._connect()) as conn:
            return {row[0] for row in conn.execute("SELECT DISTINCT room FROM light_restore_records")}
