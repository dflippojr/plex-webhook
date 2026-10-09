"""Shared fixtures: fake data only, no network, no /data or /config on the host."""
import json
import os
import socket
import sqlite3
import tempfile
from pathlib import Path

import pytest

# The app reads these paths at import time, so point them at a scratch
# directory before anything imports `app`.
_SCRATCH = Path(tempfile.mkdtemp(prefix="plex-webhook-tests-"))
os.environ["DATA_DIR"] = str(_SCRATCH / "data")
os.environ["DB_PATH"] = str(_SCRATCH / "data" / "plex_events.db")
os.environ["ROOMS_CONFIG_PATH"] = str(_SCRATCH / "rooms.yaml")
os.environ["DEVICES_SECRETS_PATH"] = str(_SCRATCH / "devices.secrets.yaml")
for _name in ("GOVEE_API_KEY", "TUYA_ACCESS_ID", "TUYA_ACCESS_KEY"):
    os.environ.pop(_name, None)

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _guard(original):
    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            raise RuntimeError(f"network access blocked in tests: {address!r}")
        return original(self, address, *args, **kwargs)

    return guarded


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Any non-loopback socket use fails the test (loopback is needed by asyncio itself)."""
    for method in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, method, _guard(getattr(socket.socket, method)))

    def no_dns(*args, **kwargs):
        raise RuntimeError("DNS lookup blocked in tests")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)


ROOMS_YAML = """
rooms:
  living_room:
    name: Living Room
    plex_clients:
      - title: Living Room TV
        uuid: uuid-living
    lights:
      - {brand: govee, id: "AA:BB", name: Lamp, model: H6159}
  bedroom:
    name: Bedroom
    plex_clients:
      - title: " Bedroom Apple TV "
        uuid: uuid-bed-tv
    lights:
      - {brand: tuya, id: plug1, name: Plug}
"""


@pytest.fixture
def rooms_file(tmp_path):
    path = tmp_path / "rooms.yaml"
    path.write_text(ROOMS_YAML, encoding="utf-8")
    return path


@pytest.fixture
def registry(rooms_file):
    """The module-level registry, pointed at the test rooms file."""
    from app.rooms import registry as reg

    reg.config_path = rooms_file
    reg.reload()
    yield reg
    reg.config_path = Path(os.environ["ROOMS_CONFIG_PATH"])
    reg._state = ({}, {}, {}, [])


@pytest.fixture
def light_calls(monkeypatch):
    """Replace the light backend with a recorder: list of (action, [light ids])."""
    from app import lights

    calls = []
    monkeypatch.setattr(
        lights,
        "apply_action",
        lambda action, room_lights, brightness=None: calls.append((action, [light["id"] for light in room_lights])),
    )
    # Reads must never reach a controller; "unreadable" makes dim fall back and restore proceed.
    monkeypatch.setattr(lights, "read_state", lambda light, timeout=None: lights.StateReading.failed())
    return calls


def _reset_restore_state(dispatcher):
    from app import db

    dispatcher._active_clients.clear()
    dispatcher._pending_rooms.clear()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM light_restore_records")
        conn.execute("DELETE FROM automation_room_owners")


@pytest.fixture(autouse=True)
def clean_dispatcher():
    from app import dispatcher

    _reset_restore_state(dispatcher)
    yield
    _reset_restore_state(dispatcher)


@pytest.fixture
def memory_db():
    from app.db import SCHEMA

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


ADMIN_TOKEN = "synthetic-admin-token-0123456789"


@pytest.fixture
def client(tmp_path, monkeypatch, registry, light_calls):
    from fastapi.testclient import TestClient
    import app.db
    import app.main as main

    # TestClient runs handlers in another thread; production audit writers own
    # their connections instead of sharing this legacy test connection.
    conn = sqlite3.connect(tmp_path / "events.db", check_same_thread=False)
    conn.executescript(app.db.SCHEMA)
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setitem(main._denial_window, "start", 0.0)
    monkeypatch.setitem(main._denial_window, "count", 0)
    monkeypatch.setattr(main, "db_conn", conn)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "EVENT_LOG", tmp_path / "events.jsonl")
    with TestClient(main.app) as c:
        c.admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        c.headers.update(c.admin)  # webhook posts ignore it; admin routes need it
        c.conn, c.log = conn, tmp_path / "events.jsonl"
        yield c
    conn.close()


def payload(event, title="Living Room TV", uuid=None, account="dan"):
    return {
        "event": event,
        "Account": {"title": account},
        "Player": {"title": title, "uuid": uuid},
        "Metadata": {"type": "movie", "title": "A Film", "ratingKey": "42"},
    }


def drain():
    """Wait for queued light actions (they run on the dispatcher's worker thread)."""
    from app import dispatcher

    dispatcher._action_executor.submit(lambda: None).result(timeout=30)


def post(client, body):
    return client.post("/webhook", data={"payload": json.dumps(body)})


def records(path):
    from app import audit_cli

    with audit_cli.open_database(path) as conn:
        args = audit_cli.parser().parse_args(["--db", str(path), "export", "--limit", "10000"])
        return list(audit_cli.read_records(conn, args))


def rows(client):
    path = client.conn.execute("PRAGMA database_list").fetchone()[2]
    return records(path)
