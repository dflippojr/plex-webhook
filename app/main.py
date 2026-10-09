import json
import os
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from app import audit, auth, dispatcher
from app.db import event_counts, get_connection, insert_event, last_received_at, list_known_clients
from app.rooms import RoomConfigError, RoomConfigUnavailable, registry

app = FastAPI(title="plex-webhook")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
EVENT_LOG = DATA_DIR / "events.jsonl"


def _env_int(name, default):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


# Request bounds for the unauthenticated webhook, sized for a Plex delivery:
# one JSON `payload` field plus an optional thumbnail.
MAX_BODY_BYTES = _env_int("WEBHOOK_MAX_BODY_BYTES", 2 * 1024 * 1024)
MAX_FORM_FILES, MAX_FORM_FIELDS = 2, 10
MAX_FIELD_BYTES = 512 * 1024
# Stored copies of the raw payload (SQLite and events.jsonl) are truncated here.
RAW_PAYLOAD_MAX_BYTES = _env_int("RAW_PAYLOAD_MAX_BYTES", 64 * 1024)


class _BodyTooLarge(Exception):
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("plex-webhook")

db_conn = get_connection()

EVENTS_TOTAL = Counter(
    "plex_webhook_events_total",
    "Total Plex webhook events received",
    ["event", "player", "account"],
)
LAST_EVENT_TIMESTAMP = Gauge(
    "plex_webhook_last_event_timestamp_seconds",
    "Unix timestamp of the last received Plex webhook event",
)

# Reuse the collector on module reload (tests reload main for counter seeding).
if "AUDIT_FAILURES_TOTAL" not in globals():
    AUDIT_FAILURES_TOTAL = Counter("plex_webhook_audit_write_failures_total", "Audit records lost to write failures")
if "WEBHOOK_REJECTED_TOTAL" not in globals():
    WEBHOOK_REJECTED_TOTAL = Counter("plex_webhook_rejected_requests_total",
                                     "Webhook requests rejected before storage", ["reason"])
for _reason in ("body_too_large", "too_many_fields", "too_many_files", "field_too_large"):
    WEBHOOK_REJECTED_TOTAL.labels(reason=_reason)
if "ADMIN_DENIALS_TOTAL" not in globals():
    ADMIN_DENIALS_TOTAL = Counter("plex_webhook_admin_denials_total", "Denied admin requests", ["reason"])
for _reason in auth.DENIAL_REASONS:
    ADMIN_DENIALS_TOTAL.labels(reason=_reason)

# Denied-request audit rows are capped per window so a flood of bad calls cannot
# grow the trail unbounded; every denial is still counted in the metric above.
DENIAL_AUDIT_LIMIT, DENIAL_AUDIT_WINDOW = 20, 60.0
_denial_window = {"start": 0.0, "count": 0}
_denial_lock = threading.Lock()


def _denial_audit_allowed():
    now = time.monotonic()
    with _denial_lock:
        if now - _denial_window["start"] >= DENIAL_AUDIT_WINDOW:
            _denial_window.update(start=now, count=0)
        _denial_window["count"] += 1
        return _denial_window["count"] <= DENIAL_AUDIT_LIMIT


def _require_admin(request, action):
    """Raise 401/503 unless the request carries the configured admin token."""
    reason = auth.denial_reason(request.headers.get("authorization"))
    if reason is None:
        return
    ADMIN_DENIALS_TOTAL.labels(reason=reason).inc()
    logger.warning("reason=admin_denied code=%s", reason)
    if _denial_audit_allowed():
        _record_audit(action=action, outcome="denied", reason_code=reason, correlation=audit.correlation_id())
    if reason == "auth_unconfigured":
        raise HTTPException(status_code=503, detail="Admin authentication unavailable")
    raise HTTPException(status_code=401, detail="Admin credential required",
                        headers={"WWW-Authenticate": "Bearer"})


def _record_audit(**fields):
    try:
        path = db_conn.execute("PRAGMA database_list").fetchone()[2]
        audit.append(path, **fields)
    except Exception:
        # Fail open. Never expose exception strings or retry webhook/light work.
        logger.warning("reason=audit_write_failed")
        AUDIT_FAILURES_TOTAL.inc()


def _light_audit_context(correlation, event_id):
    """Bind the receipt to the worker. The path is resolved here; the worker opens its own connection."""
    try:
        path = db_conn.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        logger.warning("reason=audit_write_failed")
        AUDIT_FAILURES_TOTAL.inc()
        return None

    def record(**fields):
        try:
            audit.append(path, **fields)
        except Exception:
            logger.warning("reason=audit_write_failed")
            AUDIT_FAILURES_TOTAL.inc()

    return dispatcher.AuditContext(record, correlation, event_id)


_record_audit(action="service.config_load", outcome=registry.load_outcome,
              reason_code=registry.load_reason, correlation=audit.correlation_id(),
              changed_fields=audit.configuration_changes({}, registry.rooms)
              if registry.load_outcome == "activated" else {})

# Seed from SQLite so a container restart doesn't report 0 (i.e. 1970)
_last = last_received_at(db_conn)
if _last:
    LAST_EVENT_TIMESTAMP.set(datetime.fromisoformat(_last).timestamp())

# Same for event counters, so a restart doesn't look like history was wiped
for _event, _player, _account, _count in event_counts(db_conn):
    EVENTS_TOTAL.labels(event=_event, player=_player, account=_account).inc(_count)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/clients")
async def clients(request: Request):
    _require_admin(request, "clients.read")
    """Distinct Plex clients seen so far, to help fill in the local config/rooms.yaml."""
    return {"clients": list_known_clients(db_conn)}


@app.get("/rooms")
async def rooms(request: Request):
    _require_admin(request, "rooms.read")
    return {"rooms": registry.rooms}


@app.post("/rooms/reload")
async def reload_rooms(request: Request):
    _require_admin(request, "rooms.reload")
    before = registry.rooms
    correlation = audit.correlation_id()
    _room_config_operation(registry.reload, "rooms.reload", correlation)
    _record_audit(action="rooms.reload", outcome="activated", reason_code="loaded", correlation=correlation,
                  changed_fields=audit.configuration_changes(before, registry.rooms), admin=True)
    dispatcher.init_room_gauges()
    return {"status": "reloaded", "rooms": list(registry.rooms.keys())}


def _room_config_operation(operation, action, correlation):
    try:
        return operation()
    except RoomConfigError as exc:
        _record_audit(action=action, outcome="rejected", reason_code="invalid_config", correlation=correlation, admin=True)
        raise HTTPException(status_code=422, detail=exc.detail) from None
    except RoomConfigUnavailable:
        _record_audit(action=action, outcome="rejected", reason_code="config_unavailable", correlation=correlation, admin=True)
        raise HTTPException(status_code=503, detail="Rooms configuration unavailable") from None


@app.post("/rooms/validate")
async def validate_rooms(request: Request):
    _require_admin(request, "rooms.validate")
    correlation = audit.correlation_id()
    candidate, _, _, _ = _room_config_operation(registry.validate, "rooms.validate", correlation)
    _record_audit(action="rooms.validate", outcome="validated", reason_code="valid", correlation=correlation, admin=True)
    return {"status": "valid", "rooms": list(candidate)}


@app.post("/webhook")
async def plex_webhook(request: Request):
    correlation = audit.correlation_id()
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            _record_audit(action="webhook.receipt", outcome="rejected", reason_code="invalid_form", correlation=correlation)
            raise HTTPException(status_code=400, detail="Invalid webhook form")
        if int(declared) > MAX_BODY_BYTES:
            _reject_oversized("body_too_large")
    _limit_streamed_body(request)
    try:
        form = await request.form(max_files=MAX_FORM_FILES, max_fields=MAX_FORM_FIELDS,
                                  max_part_size=MAX_FIELD_BYTES)
    except _BodyTooLarge:
        _reject_oversized("body_too_large")
    except StarletteHTTPException as exc:
        detail = str(exc.detail)
        if detail.startswith("Too many files"):
            _reject_oversized("too_many_files")
        if detail.startswith("Too many fields"):
            _reject_oversized("too_many_fields")
        if detail.startswith("Part exceeded"):
            _reject_oversized("field_too_large")
        _record_audit(action="webhook.receipt", outcome="rejected", reason_code="invalid_form", correlation=correlation)
        raise HTTPException(status_code=400, detail="Invalid webhook form") from None
    except Exception:
        _record_audit(action="webhook.receipt", outcome="rejected", reason_code="invalid_form", correlation=correlation)
        raise HTTPException(status_code=400, detail="Invalid webhook form") from None

    payload = None
    decoded_payload = None
    reason = "missing_payload"
    payload_raw = form.get("payload")
    if payload_raw is not None:
        try:
            payload = json.loads(payload_raw)
            decoded_payload = payload
            reason = "accepted" if _valid_payload(payload) else "invalid_payload"
        except (ValueError, TypeError):
            reason = "invalid_json"
            logger.warning("payload field present but not valid JSON")
    if reason != "accepted":
        payload = None

    attachments = [
        {"field": key, "filename": getattr(value, "filename", None), "content_type": getattr(value, "content_type", None)}
        for key, value in form.multi_items()
        if hasattr(value, "filename")
    ]

    event_type = payload.get("event") if payload else None
    received_at = datetime.now(timezone.utc).isoformat()

    record = {
        "received_at": received_at,
        "event": event_type,
        "payload": decoded_payload,
        "attachments": attachments,
    }

    raw_json, truncated = _bounded_raw(json.dumps(decoded_payload))
    if truncated:
        record["payload"] = raw_json
        record["payload_truncated"] = True

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    event_id = insert_event(db_conn, received_at, event_type, payload, raw_json, truncated)
    _record_audit(action="webhook.receipt", outcome="received" if reason == "accepted" else "rejected",
                  reason_code=reason, correlation=correlation, event_id=event_id)

    account_title = ((payload or {}).get("Account") or {}).get("title") or "unknown"
    player_title = ((payload or {}).get("Player") or {}).get("title") or "unknown"
    EVENTS_TOTAL.labels(event=event_type or "unknown", player=player_title, account=account_title).inc()
    LAST_EVENT_TIMESTAMP.set(time.time())

    dispatcher.handle_event(payload, _light_audit_context(correlation, event_id) if payload else None)

    logger.info("captured event=%s", event_type)
    return {"status": "received", "event": event_type}


def _reject_oversized(reason):
    """413 for a request over the bounds. Nothing is stored; the metric counts it."""
    WEBHOOK_REJECTED_TOTAL.labels(reason=reason).inc()
    logger.warning("reason=webhook_rejected code=%s", reason)
    raise HTTPException(status_code=413, detail="Webhook request too large")


def _limit_streamed_body(request):
    """Count bytes as the form parser reads them, so a missing or false Content-Length cannot bypass the cap."""
    receive, seen = request._receive, 0

    async def limited():
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > MAX_BODY_BYTES:
                raise _BodyTooLarge
        return message

    request._receive = limited


def _bounded_raw(text):
    """Cap the stored raw payload at RAW_PAYLOAD_MAX_BYTES of UTF-8; returns (text, truncated)."""
    data = text.encode("utf-8")
    if len(data) <= RAW_PAYLOAD_MAX_BYTES:
        return text, False
    return data[:RAW_PAYLOAD_MAX_BYTES].decode("utf-8", errors="ignore"), True


def _valid_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        return False
    # Reject shapes that would otherwise raise during raw capture/dispatch.
    for group, names in (("Account", ("title",)), ("Player", ("uuid", "title")), ("Metadata", ())):
        fields = payload.get(group)
        if fields is not None and not isinstance(fields, dict):
            return False
        if fields and any(fields.get(name) is not None and not isinstance(fields[name], str) for name in names):
            return False
    metadata = payload.get("Metadata") or {}
    for name in ("type", "title", "grandparentTitle", "ratingKey"):
        if metadata.get(name) is not None and not isinstance(metadata[name], (str, int, float)):
            return False
    return True
