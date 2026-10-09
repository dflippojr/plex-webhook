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
    checksum TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(recorded_at);
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_events(correlation_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action, id);
"""

# Only fixed vocabulary, generated UUIDs, numeric legacy event IDs and counts
# enter the trail. Plex account/client claims are deliberately omitted in v1.
# Actor kinds are fixed: webhook senders are the unverified ``plex-server``
# client, denied callers stay anonymous, and a valid admin token is the verified
# ``owner-admin`` client identity (see app.auth); never a request-supplied name.
ACTOR_IDS = {"plex_server": "plex-server", "admin_token": "owner-admin"}
ACTOR_VERIFIED = {"admin_token": 1}
LIGHT_FAILURE_REASONS = {
    "unsupported_brand", "missing_credentials", "missing_model", "no_address", "library_unavailable",
    "send_error", "request_failed", "tuya_rejected", "unexpected_error",
    "light_off", "manual_change", "no_record",
}
# Per-light brightness decisions: outcome is the decision, reason the evidence behind it.
LIGHT_DECISIONS = {
    "restored": {"within_tolerance"},
    "skipped_manual_change": {"brightness_changed", "turned_off"},
    "skipped_was_off": {"was_off"},
    "restore_without_read": {"read_failed"},
    "dim_recorded": {"read_ok", "read_failed", "at_dim_level"},
    "dim_skipped_off": {"was_off"},
    "dim_kept_original": {"record_exists"},
}
OPERATIONS = {
    "webhook.receipt": ("webhook", "plex_server", "webhook", {
        "received": {"accepted"},
        "rejected": {"missing_payload", "invalid_json", "invalid_payload", "invalid_form"},
    }),
    "rooms.reload": ("http", "anonymous", "rooms", {
        "activated": {"loaded"}, "rejected": {"invalid_config", "config_unavailable"},
        "denied": {"auth_unconfigured", "missing_credential", "invalid_credential"},
    }),
    "rooms.validate": ("http", "anonymous", "rooms", {
        "validated": {"valid"}, "rejected": {"invalid_config", "config_unavailable"},
        "denied": {"auth_unconfigured", "missing_credential", "invalid_credential"},
    }),
    "rooms.read": ("http", "anonymous", "rooms", {"denied": {"auth_unconfigured", "missing_credential", "invalid_credential"},}),
    "clients.read": ("http", "anonymous", "clients", {"denied": {"auth_unconfigured", "missing_credential", "invalid_credential"},}),
    "service.config_load": ("service", "system", "rooms", {
        "activated": {"loaded"}, "rejected": {"invalid_config", "config_unavailable"},
    }),
    # Light-action records share the originating receipt's correlation ID and carry the
    # initiating actor in ``detail.on_behalf_of``. Outcomes are transport evidence, never
    # proof of physical state: command_sent / request_accepted_by_transport / unconfirmed.
    "light.action_queued": ("dispatcher", "system", "room", {"queued": {"accepted"}}),
    "light.result": ("dispatcher", "system", "light", {
        "command_sent": {"lan_command_sent"},
        "request_accepted_by_transport": {"local_accepted", "cloud_accepted"},
        "unconfirmed": {"result_unconfirmed"},
        "failed": LIGHT_FAILURE_REASONS,
        "skipped": LIGHT_FAILURE_REASONS,
    }),
    "light.decision": ("dispatcher", "system", "light", LIGHT_DECISIONS),
    "light.action_summary": ("dispatcher", "system", "room", {
        "completed_unverified": {"all_sent"}, "partial": {"mixed_results"},
        "unconfirmed": {"all_unconfirmed", "some_unconfirmed", "no_results"},
        "failed": {"all_failed", "dispatcher_error"}, "skipped": {"all_skipped", "no_lights"},
    }),
}
LIGHT_REQUESTS = {"dim", "restore"}
LIGHT_TRANSPORTS = {"lan", "local", "cloud", "none"}
LIGHT_CREDENTIAL_SOURCES = {"none", "environment", "device_config"}
LIGHT_STEPS = ("turn", "brightness")
LABEL_LIMIT = 128
CHANGE_PATHS = {"rooms", "rooms.count", "rooms.plex_clients", "rooms.lights", "rooms.other"}
DETAIL_ACTIONS = {"light.action_queued", "light.result", "light.decision", "light.action_summary"}
RECORD_COLUMNS = ("recorded_at", "correlation_id", "source", "actor_kind", "actor_id", "actor_verified",
                  "action", "target_kind", "target_id", "outcome", "reason_code", "changed_fields", "checksum")
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


def _label(value):
    """Configured IDs only: bounded strings, never request or exception text."""
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid audit label")
    return value[:LABEL_LIMIT]


def _choice(value, allowed):
    if value not in allowed:
        raise ValueError("Invalid audit detail")
    return value


def _steps(value):
    value = list(value)
    if any(step not in LIGHT_STEPS for step in value) or len(set(value)) != len(value):
        raise ValueError("Invalid audit detail")
    return [step for step in LIGHT_STEPS if step in value]


def _number(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Invalid audit detail")
    return value


def clean_detail(action, detail):
    """Rebuild detail from fixed vocabulary; unknown keys and free text never pass through."""
    detail = dict(detail)
    result_outcomes = OPERATIONS["light.result"][3]
    cleaned = {"action_id": str(UUID(detail["action_id"])), "request": _choice(detail["request"], LIGHT_REQUESTS)}
    actor = _choice(detail["on_behalf_of"], ACTOR_IDS)
    cleaned["on_behalf_of"] = {"kind": actor, "id": ACTOR_IDS[actor], "verified": ACTOR_VERIFIED.get(actor, 0)}
    if detail.get("event_id") is not None:
        cleaned["event_id"] = _number(detail["event_id"], 2 ** 63 - 1)
    if action == "light.action_queued":
        cleaned["targets"] = [
            {"brand": _label(t["brand"]), "id": _label(t["id"]),
             "brightness": None if t["brightness"] is None else _number(t["brightness"], 100)}
            for t in detail["targets"]
        ]
    elif action == "light.result":
        cleaned["brand"] = _label(detail["brand"])
        cleaned["transport"] = _choice(detail["transport"], LIGHT_TRANSPORTS)
        cleaned["credential_source"] = _choice(detail["credential_source"], LIGHT_CREDENTIAL_SOURCES)
        cleaned["progress"] = _steps(detail["progress"])
        cleaned["attempts"] = [
            {"transport": _choice(a["transport"], LIGHT_TRANSPORTS),
             "credential_source": _choice(a["credential_source"], LIGHT_CREDENTIAL_SOURCES),
             "outcome": _choice(a["outcome"], result_outcomes),
             "reason_code": _choice(a["reason_code"], set().union(*result_outcomes.values())),
             "progress": _steps(a["progress"])}
            for a in detail["attempts"]
        ]
    elif action == "light.decision":
        cleaned["brand"] = _label(detail["brand"])
        for key in ("dim_percent", "restore_percent", "observed_percent"):
            value = detail.get(key)
            cleaned[key] = None if value is None else _number(value, 100)
    else:
        cleaned["counts"] = {
            _choice(key, result_outcomes): _number(value, 10 ** 6) for key, value in dict(detail["counts"]).items()
        }
    return cleaned


def append(path, *, action, outcome, reason_code, correlation, changed_fields=None, event_id=None,
           admin=False, target_id=None, detail=None):
    """Insert one validated record using an owned connection and bounded lock wait.

    A later worker may call this contract with its own path/correlation; it must
    never share main.db_conn. Only the operations above are supported in v1.
    """
    source, actor, target, outcomes = OPERATIONS[action]
    if admin:
        if outcome == "denied":
            raise ValueError("Denied callers are never verified")
        actor = "admin_token"
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
    if target_id is not None and target not in {"room", "light"}:
        raise ValueError("Invalid audit target")
    if (detail is None) == (action in DETAIL_ACTIONS):
        raise ValueError("Invalid audit detail")
    cleaned_detail = clean_detail(action, detail) if detail is not None else None
    record = dict(recorded_at=utc_now(), correlation_id=correlation, source=source,
                  actor_kind=actor, actor_id=ACTOR_IDS.get(actor),
                  actor_verified=ACTOR_VERIFIED.get(actor, 0), action=action,
                  target_kind="event" if event_id is not None else target,
                  target_id=str(event_id) if event_id is not None else (_label(target_id) if target_id else None),
                  outcome=outcome, reason_code=reason_code, changed_fields=changes)
    if cleaned_detail is not None:
        record["detail"] = cleaned_detail  # legacy rows have no detail key, so their checksums stay valid
    record["checksum"] = checksum(record)
    record["changed_fields"] = json.dumps(changes, sort_keys=True, separators=(",", ":"))
    detail_json = json.dumps(cleaned_detail, sort_keys=True, separators=(",", ":")) if cleaned_detail else None
    # mode=rw prevents accidentally creating a second database after a bad path.
    uri = Path(path).resolve().as_uri() + "?mode=rw"
    with WRITE_LOCK, closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as conn:
        conn.executescript(SCHEMA)
        if "detail" not in {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}:
            conn.execute("ALTER TABLE audit_events ADD COLUMN detail TEXT")  # table predates light records
        with conn:
            cursor = conn.execute(
                """INSERT INTO audit_events (
                    recorded_at, correlation_id, source, actor_kind, actor_id, actor_verified,
                    action, target_kind, target_id, outcome, reason_code, changed_fields, checksum, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(record[key] for key in RECORD_COLUMNS) + (detail_json,),
            )
        return cursor.lastrowid
