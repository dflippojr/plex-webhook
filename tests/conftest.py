"""Shared fixtures: fake data only, no network, no /data or /config on the host.

Every test that needs the service builds its own (``service``, ``playback``, ``client``)
from ``settings`` that point into its tmp_path. Importing ``app`` opens nothing.
"""
import dataclasses
import json
import os
import socket
import sqlite3

import pytest

# Device credentials are read where they are used; none may leak in from the host.
for _name in ("GOVEE_API_KEY", "TUYA_ACCESS_ID", "TUYA_ACCESS_KEY", "HASS_URL", "HASS_TOKEN", "WEBHOOK_TOKEN"):
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


@pytest.fixture(autouse=True)
def empty_govee_address_cache(monkeypatch):
    """Discovered Govee addresses never leak from one test into the next."""
    from app import lights

    monkeypatch.setattr(lights, "_govee_addresses", {})


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
def settings(tmp_path, rooms_file):
    """Settings for a service whose files all live in this test's tmp_path."""
    from app.settings import Settings

    return Settings(db_path=tmp_path / "data" / "plex_events.db", rooms_config_path=rooms_file,
                    devices_secrets_path=tmp_path / "devices.secrets.yaml")


# Services the running test built, newest last; drain() waits on the newest one's queue.
_services = []


@pytest.fixture
def make_service(settings):
    """Build and start a Service (no HTTP app); every one built is stopped after the test."""
    from app.service import Service

    built = []

    def make(service_settings=settings):
        service = Service(service_settings).start()
        built.append(service)
        _services.append(service)
        return service

    yield make
    for service in built:
        _services.remove(service)
        service.stop()


@pytest.fixture
def service(request, make_service):
    """The test's Service: the app's when the test also uses ``client``, else one built for it."""
    if "client" in request.fixturenames:
        return request.getfixturevalue("client").service
    return make_service()


@pytest.fixture
def registry(service):
    """The service's room registry, loaded from the test rooms file."""
    return service.registry


@pytest.fixture
def playback(service):
    """The service's playback state: player state, restore records and the light-action queue."""
    return service.playback


@pytest.fixture
def restore_records(service):
    """The service's restore records (app.db.RestoreRecords)."""
    return service.records


def accepted_result(light):
    """The result a brand reports when its command goes through: Govee LAN sends, Tuya local is accepted."""
    from app import lights

    if light.get("brand") == "govee":
        transport, outcome, reason = "lan", "command_sent", "lan_command_sent"
    else:
        transport, outcome, reason = "local", "request_accepted_by_transport", "local_accepted"
    return lights.LightResult(light.get("brand"), light["id"], transport, "none", outcome, reason)


@pytest.fixture
def light_calls(monkeypatch):
    """Replace the light backend with a recorder: list of (action, [light ids]).

    Like the real apply_action, it returns a LightResult per light: the transport
    outcome each brand reports when its command goes through.
    """
    from app import lights

    calls = []

    def apply_action(action, room_lights, brightness=None, turn_on=True):
        calls.append((action, [light["id"] for light in room_lights]))
        return [accepted_result(light) for light in room_lights]

    monkeypatch.setattr(lights, "apply_action", apply_action)
    # Reads must never reach a controller; "unreadable" makes dim fall back and restore proceed.
    monkeypatch.setattr(lights, "read_state", lambda light, timeout=None: lights.StateReading.failed())
    monkeypatch.setattr(lights, "prepare", lambda room_lights: None)
    return calls


@pytest.fixture
def memory_db():
    from app.db import SCHEMA

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def migrated(conn):
    """Bring a test connection's file to the latest schema, as service startup does."""
    from app import storage

    storage.migrate(conn)
    return conn


ADMIN_TOKEN = "synthetic-admin-token-0123456789"
WEBHOOK_TOKEN = "synthetic-webhook-token-0123456789abcdef"

# Modules whose tests also run with WEBHOOK_TOKEN set: every `client` webhook post
# then goes to /webhook/<token>, so the same journeys prove the tokened route.
TOKEN_JOURNEY_MODULES = {
    "test_admin_auth", "test_audit_http", "test_expiry", "test_govee_http", "test_light_audit", "test_main",
    "test_media_types", "test_playback_http",
}


def pytest_generate_tests(metafunc):
    if "client" in metafunc.fixturenames and metafunc.module.__name__.rpartition(".")[2] in TOKEN_JOURNEY_MODULES:
        metafunc.parametrize("client", ["no_token", "token"], indirect=True)


def _token_test_client():
    from fastapi.testclient import TestClient

    class TokenClient(TestClient):
        """Sends POST /webhook to /webhook/<token>; every other request is unchanged."""

        def request(self, method, url, *args, **kwargs):
            if method.upper() == "POST" and url == "/webhook":
                url = f"/webhook/{WEBHOOK_TOKEN}"
            return super().request(method, url, *args, **kwargs)

    return TokenClient


def client_settings(request, settings):
    """``settings``, with the webhook token configured for the token journeys."""
    from app import auth

    if getattr(request, "param", "no_token") != "token":
        return settings
    return dataclasses.replace(settings, webhook_token_digest=auth.webhook_token_digest(WEBHOOK_TOKEN))


@pytest.fixture
def client(request, tmp_path, monkeypatch, settings, light_calls):
    """A TestClient on a freshly built app; ``client.service`` is the Service its lifespan started."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    test_settings = client_settings(request, settings)
    if test_settings.webhook_token_digest is not None:
        TestClient = _token_test_client()
    app = create_app(test_settings)
    with TestClient(app) as c:
        c.service = app.state.service
        _services.append(c.service)
        c.admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        c.headers.update(c.admin)  # webhook posts ignore it; admin routes need it
        # The test's own read connection to the service's database file.
        c.conn = conn = sqlite3.connect(test_settings.db_path, check_same_thread=False)
        c.log = tmp_path / "events.jsonl"
        try:
            yield c
        finally:
            _services.remove(c.service)
    try:
        # The retired events.jsonl is never written again (#183).
        assert not c.log.exists()
        # Every stored copy must be standard JSON (no NaN/Infinity tokens); a truncated payload is cut text.
        for raw, truncated, attachments in conn.execute("SELECT raw_payload, raw_truncated, attachments FROM events"):
            if not truncated:
                json.loads(raw, parse_constant=_reject_constant)
            if attachments is not None:
                json.loads(attachments, parse_constant=_reject_constant)
    finally:
        conn.close()


def _reject_constant(constant):
    raise ValueError(f"non-standard JSON constant {constant}")


def payload(event, title="Living Room TV", uuid=None, account="dan"):
    return {
        "event": event,
        "Account": {"title": account},
        "Player": {"title": title, "uuid": uuid},
        "Metadata": {"type": "movie", "title": "A Film", "ratingKey": "42"},
    }


def active_service():
    """The newest service the running test built, for module helpers that take no fixture."""
    return _services[-1]


def drain(service=None):
    """Wait for queued light actions (they run on the service's worker thread); defaults to the newest service."""
    (service or active_service()).queue.drain(timeout=30)


def post(client, body):
    return client.post("/webhook", data={"payload": json.dumps(body)})


def records(path):
    from app import audit_cli

    with audit_cli.open_database(path) as conn:
        args = audit_cli.parser().parse_args(["--db", str(path), "export", "--limit", "10000"])
        return list(audit_cli.read_records(conn, args))


def rows(client):
    """The client's audit trail after startup: the app's own service.config_load record is left out."""
    path = client.conn.execute("PRAGMA database_list").fetchone()[2]
    return [record for record in records(path) if record["action"] != "service.config_load"]
