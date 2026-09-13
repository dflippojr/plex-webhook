import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request

app = FastAPI(title="plex-webhook")

DATA_DIR = Path("/data")
EVENT_LOG = DATA_DIR / "events.jsonl"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("plex-webhook")


@app.get("/health")
async def health():
    return {"status": "ok"}


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

    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "event": payload.get("event") if payload else None,
        "payload": payload,
        "attachments": attachments,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    logger.info("captured event=%s", record["event"])
    return {"status": "received", "event": record["event"]}
