"""Append contract for safe audit records; no app, device or payload imports.

Every append owns its connection. Callers must handle failures without retrying
their side effects. There is deliberately no update/delete API here.
"""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_id TEXT,
    actor_verified INTEGER NOT NULL CHECK(actor_verified IN (0, 1)),
    action TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    changed_fields TEXT NOT NULL,
    checksum TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(recorded_at);
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_events(correlation_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action, id);
"""

# Only fixed vocabulary, generated UUIDs, numeric legacy event IDs and counts
# enter the trail. Plex account/client claims are deliberately omitted in v1.
OPERATIONS = {
    "webhook.receipt": ("webhook", "anonymous", "webhook", {
        "received": {"accepted"},
        "rejected": {"missing_payload", "invalid_json", "invalid_payload", "invalid_form"},
    }),
    "rooms.reload": ("http", "anonymous", "rooms", {
        "activated": {"loaded"}, "rejected": {"invalid_config", "config_unavailable"},
    }),
    "rooms.validate": ("http", "anonymous", "rooms", {
        "validated": {"valid"}, "rejected": {"invalid_config", "config_unavailable"},
    }),
    "service.config_load": ("service", "system", "rooms", {
        "activated": {"loaded"}, "rejected": {"invalid_config", "config_unavailable"},
    }),
}
CHANGE_PATHS = {"rooms", "rooms.count", "rooms.plex_clients", "rooms.lights", "rooms.other"}
WRITE_LOCK = RLock()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def correlation_id():
    return str(uuid4())


def checksum(record):
    """Unkeyed per-record checksum; not proof against a machine owner."""
    content = {key: value for key, value in record.items() if key not in {"id", "checksum"}}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def configuration_changes(before, after):
    """Counts of changed room groups; paths never contain room names or values."""
    result = {}
    keys = set(before) | set(after)
    if set(before) != set(after):
        result["rooms"] = len(set(before) ^ set(after))
    if len(before) != len(after):
        result["rooms.count"] = abs(len(before) - len(after))
    for field in ("plex_clients", "lights"):
        count = sum(before.get(key, {}).get(field, []) != after.get(key, {}).get(field, []) for key in keys)
        if count:
            result[f"rooms.{field}"] = count
    count = sum(
        {k: v for k, v in before.get(key, {}).items() if k not in {"plex_clients", "lights"}}
        != {k: v for k, v in after.get(key, {}).items() if k not in {"plex_clients", "lights"}}
        for key in keys
    )
    if count:
        result["rooms.other"] = count
    return result


def append(path, *, action, outcome, reason_code, correlation, changed_fields=None, event_id=None):
    """Insert one validated record using an owned connection and bounded lock wait.

    A later worker may call this contract with its own path/correlation; it must
    never share main.db_conn. Only the operations above are supported in v1.
    """
    source, actor, target, outcomes = OPERATIONS[action]
    if reason_code not in outcomes[outcome]:
        raise ValueError("Invalid audit result")
    correlation = str(UUID(correlation))
    changes = dict(changed_fields or {})
    if any(key not in CHANGE_PATHS or type(value) is not int or value < 0 for key, value in changes.items()):
        raise ValueError("Invalid audit changes")
    if changes and outcome != "activated":
        raise ValueError("Only activation records changes")
    if event_id is not None and (action != "webhook.receipt" or type(event_id) is not int or event_id < 1):
        raise ValueError("Invalid audit target")
    record = dict(recorded_at=utc_now(), correlation_id=correlation, source=source,
                  actor_kind=actor, actor_id=None, actor_verified=0, action=action,
                  target_kind="event" if event_id is not None else target,
                  target_id=str(event_id) if event_id is not None else None,
                  outcome=outcome, reason_code=reason_code, changed_fields=changes)
    record["checksum"] = checksum(record)
    record["changed_fields"] = json.dumps(changes, sort_keys=True, separators=(",", ":"))
    # mode=rw prevents accidentally creating a second database after a bad path.
    uri = Path(path).resolve().as_uri() + "?mode=rw"
    with WRITE_LOCK, closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as conn:
        conn.executescript(SCHEMA)
        with conn:
            cursor = conn.execute(
                """INSERT INTO audit_events (
                    recorded_at, correlation_id, source, actor_kind, actor_id, actor_verified,
                    action, target_kind, target_id, outcome, reason_code, changed_fields, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(record.values()),
            )
        return cursor.lastrowid
