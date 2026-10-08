import json
import os
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from app import audit, dispatcher
from app.db import event_counts, get_connection, insert_event, last_received_at, list_known_clients
from app.rooms import RoomConfigError, RoomConfigUnavailable, registry

app = FastAPI(title="plex-webhook")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
EVENT_LOG = DATA_DIR / "events.jsonl"

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


def _record_audit(**fields):
    try:
        path = db_conn.execute("PRAGMA database_list").fetchone()[2]
        audit.append(path, **fields)
    except Exception:
        # Fail open. Never expose exception strings or retry webhook/light work.
        logger.warning("reason=audit_write_failed")
        AUDIT_FAILURES_TOTAL.inc()


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
async def clients():
    """Distinct Plex clients seen so far, to help fill in the local config/rooms.yaml."""
    return {"clients": list_known_clients(db_conn)}


@app.get("/rooms")
async def rooms():
    return {"rooms": registry.rooms}


@app.post("/rooms/reload")
async def reload_rooms():
    before = registry.rooms
    correlation = audit.correlation_id()
    _room_config_operation(registry.reload, "rooms.reload", correlation)
    _record_audit(action="rooms.reload", outcome="activated", reason_code="loaded", correlation=correlation,
                  changed_fields=audit.configuration_changes(before, registry.rooms))
    dispatcher.init_room_gauges()
    return {"status": "reloaded", "rooms": list(registry.rooms.keys())}


def _room_config_operation(operation, action, correlation):
    try:
        return operation()
    except RoomConfigError as exc:
        _record_audit(action=action, outcome="rejected", reason_code="invalid_config", correlation=correlation)
        raise HTTPException(status_code=422, detail=exc.detail) from None
    except RoomConfigUnavailable:
        _record_audit(action=action, outcome="rejected", reason_code="config_unavailable", correlation=correlation)
        raise HTTPException(status_code=503, detail="Rooms configuration unavailable") from None


@app.post("/rooms/validate")
async def validate_rooms():
    correlation = audit.correlation_id()
    candidate, _, _ = _room_config_operation(registry.validate, "rooms.validate", correlation)
    _record_audit(action="rooms.validate", outcome="validated", reason_code="valid", correlation=correlation)
    return {"status": "valid", "rooms": list(candidate)}


@app.post("/webhook")
async def plex_webhook(request: Request):
    correlation = audit.correlation_id()
    try:
        form = await request.form()
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

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    event_id = insert_event(db_conn, received_at, event_type, payload, json.dumps(decoded_payload))
    _record_audit(action="webhook.receipt", outcome="received" if reason == "accepted" else "rejected",
                  reason_code=reason, correlation=correlation, event_id=event_id)

    account_title = ((payload or {}).get("Account") or {}).get("title") or "unknown"
    player_title = ((payload or {}).get("Player") or {}).get("title") or "unknown"
    EVENTS_TOTAL.labels(event=event_type or "unknown", player=player_title, account=account_title).inc()
    LAST_EVENT_TIMESTAMP.set(time.time())

    dispatcher.handle_event(payload)

    logger.info("captured event=%s", event_type)
    return {"status": "received", "event": event_type}


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
