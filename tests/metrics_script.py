"""Drive a fresh service through a fixed sequence of events and print its /metrics output.

Run in a subprocess by tests/test_metrics_golden.py, so the service starts from an empty
database and fresh collectors. It uses only the public surface (``app.main.app`` and
HTTP routes), so the same script runs against any version of the service. Every light
uses an unknown brand, so no command can leave the process; sockets are blocked anyway.

Usage: python tests/metrics_script.py <scratch directory>
"""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

ROOMS_YAML = """
rooms:
  den:
    plex_clients:
      - {title: Den TV, uuid: uuid-den}
    lights:
      - {brand: synthetic, id: den-1, name: Lamp}
  attic:
    plex_clients:
      - {title: Attic TV}
    lights:
      - {brand: synthetic, id: attic-1}
automations:
  - trigger: {room: den, event: dim}
    target_room: attic
    action: 40
  - trigger: {room: den, event: restore}
    target_room: attic
    action: restore
"""
ADMIN = {"Authorization": "Bearer synthetic-admin-token-0123456789"}
VOLATILE_PREFIXES = ("process_", "python_gc_", "python_info", "plex_webhook_last_event_timestamp_seconds")


def _blocked(*args, **kwargs):
    raise RuntimeError("network access blocked in the metrics script")


def _event(event, title, uuid=None, account="synthetic"):
    return {"payload": json.dumps({"event": event, "Account": {"title": account},
                                   "Player": {"title": title, "uuid": uuid},
                                   "Metadata": {"type": "movie", "title": "A Film"}})}


def normalize(text):
    """Mask values that differ between runs or interpreters; keep every name, HELP and TYPE line."""
    lines = []
    for line in text.splitlines():
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if line.startswith("#") or not line:
            lines.append(line)
        elif name.startswith(VOLATILE_PREFIXES):
            lines.append(f"{name} <volatile>")
        elif name.endswith("_created"):
            lines.append(f"{line.rsplit(' ', 1)[0]} <created>")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def run(scratch):
    scratch = Path(scratch)
    (scratch / "rooms.yaml").write_text(ROOMS_YAML, encoding="utf-8")
    os.environ.update(DB_PATH=str(scratch / "data" / "events.db"), ROOMS_CONFIG_PATH=str(scratch / "rooms.yaml"),
                      DEVICES_SECRETS_PATH=str(scratch / "devices.secrets.yaml"),
                      ADMIN_API_TOKEN=ADMIN["Authorization"].split()[1])
    for name in ("WEBHOOK_TOKEN", "GOVEE_API_KEY", "TUYA_ACCESS_ID", "TUYA_ACCESS_KEY"):
        os.environ.pop(name, None)
    for method in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, method, _blocked)
    socket.getaddrinfo = _blocked

    return normalize(asyncio.run(_drive()))


async def _drive():
    # In-process ASGI on this thread, as uvicorn runs the app: the startup connection stays on its thread.
    import httpx
    from app.main import app

    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://plex-webhook.test") as client:
        steps = [
            _event("media.play", "Den TV", "uuid-den"),
            _event("media.pause", "Den TV", "uuid-den"),
            _event("media.resume", "Den TV", "uuid-den"),
            _event("media.play", "Attic TV"),
            _event("media.stop", "Attic TV"),
            _event("media.stop", "Den TV", "uuid-den"),
            _event("media.scrobble", "Elsewhere TV", account="other"),
            _event("synthetic.event", "Den TV", "uuid-den"),
            {"payload": "not json"},
        ]
        for data in steps:
            assert (await client.post("/webhook", data=data)).status_code == 200
            # A light check runs behind every light action queued before it, so no action coalesces.
            assert (await client.get("/rooms/den/lights", headers=ADMIN)).status_code == 200
        oversized = await client.post("/webhook", content=b"x" * 10,
                                      headers={"content-length": str(64 * 1024 * 1024)})
        assert oversized.status_code == 413
        assert (await client.get("/rooms")).status_code == 401
        assert (await client.get("/rooms", headers={"Authorization": "Bearer wrong"})).status_code == 401
        reload = await client.post("/rooms/reload", headers=ADMIN)
        assert reload.status_code == 200, reload.text
        return (await client.get("/metrics")).text


if __name__ == "__main__":
    sys.stdout.write(run(sys.argv[1]))
