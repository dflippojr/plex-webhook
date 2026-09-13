import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from app import dispatcher
from app.db import get_connection, insert_event, list_known_clients
from app.rooms import registry

app = FastAPI(title="plex-webhook")

DATA_DIR = Path("/data")
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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/clients")
async def clients():
    """Distinct Plex clients seen so far, to help fill in config/rooms.yaml."""
    return {"clients": list_known_clients(db_conn)}


@app.get("/rooms")
async def rooms():
    return {"rooms": registry.rooms}


@app.post("/rooms/reload")
async def reload_rooms():
    registry.reload()
    return {"status": "reloaded", "rooms": list(registry.rooms.keys())}


@app.post("/webhook")
async def plex_webhook(request: Request):
    form = await request.form()

    payload = None
    payload_raw = form.get("payload")
    if payload_raw is not None:
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            logger.warning("payload field present but not valid JSON")

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
        "payload": payload,
        "attachments": attachments,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    insert_event(db_conn, received_at, event_type, payload, json.dumps(payload))

    account_title = (payload or {}).get("Account", {}).get("title") or "unknown"
    player_title = (payload or {}).get("Player", {}).get("title") or "unknown"
    EVENTS_TOTAL.labels(event=event_type or "unknown", player=player_title, account=account_title).inc()
    LAST_EVENT_TIMESTAMP.set(time.time())

    dispatcher.handle_event(payload)

    logger.info("captured event=%s", event_type)
    return {"status": "received", "event": event_type}
