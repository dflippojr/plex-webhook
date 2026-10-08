import importlib
import json
import sqlite3

import pytest
from prometheus_client import REGISTRY

from conftest import drain, payload


def post(client, body):
    return client.post("/webhook", data={"payload": json.dumps(body)})


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_webhook_valid_payload_records_and_dispatches(client, light_calls):
    resp = post(client, payload("media.play"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "received", "event": "media.play"}
    assert client.conn.execute("SELECT event, player_title, title FROM events").fetchall() == [
        ("media.play", "Living Room TV", "A Film")
    ]
    assert json.loads(client.log.read_text().splitlines()[0])["event"] == "media.play"
    drain()
    drain()
    assert light_calls == [("dim", ["AA:BB"])]


def test_webhook_invalid_json_still_200_and_recorded(client, light_calls):
    resp = client.post("/webhook", data={"payload": "{not json"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "received", "event": None}
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    drain()
    assert light_calls == []


def test_webhook_without_payload_field(client):
    resp = client.post("/webhook", data={"other": "x"})
    assert resp.status_code == 200
    assert resp.json()["event"] is None
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_webhook_with_attachment_is_logged(client):
    resp = client.post(
        "/webhook",
        data={"payload": json.dumps(payload("media.stop"))},
        files={"thumb": ("t.jpg", b"jpgbytes", "image/jpeg")},
    )
    assert resp.status_code == 200
    record = json.loads(client.log.read_text().splitlines()[0])
    assert record["attachments"] == [{"field": "thumb", "filename": "t.jpg", "content_type": "image/jpeg"}]


def test_webhook_increments_metrics(client):
    labels = {"event": "media.pause", "player": "Metrics TV", "account": "dan"}
    before = REGISTRY.get_sample_value("plex_webhook_events_total", labels) or 0
    post(client, payload("media.pause", title="Metrics TV"))
    assert REGISTRY.get_sample_value("plex_webhook_events_total", labels) == before + 1
    assert b"plex_webhook_events_total" in client.get("/metrics").content


def test_clients_lists_seen_players(client):
    post(client, payload("media.play", "Living Room TV", "uuid-living"))
    post(client, payload("media.stop", "Living Room TV", "uuid-living"))
    clients = client.get("/clients").json()["clients"]
    assert len(clients) == 1
    assert clients[0]["title"] == "Living Room TV"
    assert clients[0]["uuid"] == "uuid-living"
    assert clients[0]["event_count"] == 2


def test_rooms_and_reload(client, rooms_file):
    assert set(client.get("/rooms").json()["rooms"]) == {"living_room", "bedroom"}
    rooms_file.write_text("rooms:\n  den:\n    plex_clients: []\n    lights: []\n", encoding="utf-8")
    resp = client.post("/rooms/reload")
    assert resp.json() == {"status": "reloaded", "rooms": ["den"]}
    assert list(client.get("/rooms").json()["rooms"]) == ["den"]


def test_validate_does_not_publish_or_dispatch(client, registry, rooms_file, monkeypatch, light_calls):
    from app import dispatcher

    post(client, payload("media.play", uuid="uuid-living"))
    drain()
    before = registry._state
    active = {key: set(value) for key, value in dispatcher._active_clients.items()}
    metrics = list(dispatcher.ROOM_ACTIVE_SESSIONS.collect())[0].samples
    calls = list(light_calls)
    def unexpected():
        pytest.fail("validation must not initialize gauges")
    original_init = dispatcher.init_room_gauges
    monkeypatch.setattr(dispatcher, "init_room_gauges", unexpected)
    rooms_file.write_text("rooms: {den: {}}", encoding="utf-8")
    assert client.post("/rooms/validate").json() == {"status": "valid", "rooms": ["den"]}
    assert registry._state is before
    assert dispatcher._active_clients == active
    assert list(dispatcher.ROOM_ACTIVE_SESSIONS.collect())[0].samples == metrics
    assert light_calls == calls
    monkeypatch.setattr(dispatcher, "init_room_gauges", original_init)
    assert client.post("/rooms/reload").json() == {"status": "reloaded", "rooms": ["den"]}


@pytest.mark.parametrize("endpoint", ["validate", "reload"])
@pytest.mark.parametrize("source", [
    "rooms: [private-value", "rooms: {den: {plex_clients: [42]}}",
    "rooms: {den: {}, den: {}}",
    "rooms: {a: {plex_clients: [{uuid: private-value}]}, b: {plex_clients: [{uuid: private-value}]}}",
    "rooms: {a: {plex_clients: [{title: Private-Value}]}, b: {plex_clients: [{title: ' private-value '}]}}",
])
def test_invalid_room_endpoints_preserve_dispatch(client, registry, rooms_file, light_calls, monkeypatch, endpoint, source):
    from app import dispatcher

    post(client, payload("media.play", uuid="uuid-living"))
    drain()
    before = registry._state
    active = {key: set(value) for key, value in dispatcher._active_clients.items()}
    metrics = list(dispatcher.ROOM_ACTIVE_SESSIONS.collect())[0].samples
    calls = list(light_calls)
    def unexpected():
        pytest.fail("failed candidates must not initialize gauges")
    monkeypatch.setattr(dispatcher, "init_room_gauges", unexpected)
    rooms_file.write_text(source, encoding="utf-8")
    resp = client.post(f"/rooms/{endpoint}")
    assert resp.status_code == 422
    assert all(set(error) == {"path", "code"} for error in resp.json()["detail"])
    assert "private-value" not in resp.text.lower()
    assert registry._state is before
    assert dispatcher._active_clients == active
    assert list(dispatcher.ROOM_ACTIVE_SESSIONS.collect())[0].samples == metrics
    assert light_calls == calls
    post(client, payload("media.stop", uuid="uuid-living"))
    drain()
    assert light_calls[-1] == ("restore", ["AA:BB"])


@pytest.mark.parametrize("endpoint", ["validate", "reload"])
@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_unavailable_room_endpoints(client, registry, monkeypatch, endpoint, failure):
    from pathlib import Path
    from app import dispatcher

    before = registry._state
    def fail(*args, **kwargs):
        raise failure("private-value")
    def unexpected():
        pytest.fail("unavailable config must not initialize gauges")
    monkeypatch.setattr(Path, "read_text", fail)
    monkeypatch.setattr(dispatcher, "init_room_gauges", unexpected)
    resp = client.post(f"/rooms/{endpoint}")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Rooms configuration unavailable"}
    assert registry._state is before


def test_invalid_startup_still_captures_webhook(client, rooms_file, monkeypatch, light_calls):
    from app import dispatcher
    import app.main as main
    from app.rooms import RoomRegistry

    rooms_file.write_text("rooms: [private-value", encoding="utf-8")
    startup = RoomRegistry(rooms_file)
    monkeypatch.setattr(main, "registry", startup)
    monkeypatch.setattr(dispatcher, "registry", startup)
    assert post(client, payload("media.play")).status_code == 200
    assert json.loads(client.log.read_text().splitlines()[0])["event"] == "media.play"
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    drain()
    assert light_calls == []


def test_counters_and_timestamp_seeded_from_sqlite_on_startup(tmp_path, monkeypatch):
    import app.db
    import app.main as main

    db_path = tmp_path / "seed.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(app.db.SCHEMA)
    for event in ("media.play", "media.play", "media.stop"):
        app.db.insert_event(conn, "2024-05-01T12:00:00+00:00", event, payload(event, "Seed TV"), "{}")
    conn.close()

    # Re-import the app module against the seeded database, dropping the old collectors first.
    for collector in (main.EVENTS_TOTAL, main.LAST_EVENT_TIMESTAMP):
        REGISTRY.unregister(collector)
    monkeypatch.setattr(app.db, "DB_PATH", db_path)
    try:
        reloaded = importlib.reload(main)
        labels = {"player": "Seed TV", "account": "dan"}
        assert REGISTRY.get_sample_value("plex_webhook_events_total", {"event": "media.play", **labels}) == 2
        assert REGISTRY.get_sample_value("plex_webhook_events_total", {"event": "media.stop", **labels}) == 1
        assert REGISTRY.get_sample_value("plex_webhook_last_event_timestamp_seconds") == 1714564800.0
        reloaded.db_conn.close()
    finally:
        for collector in (main.EVENTS_TOTAL, main.LAST_EVENT_TIMESTAMP):
            REGISTRY.unregister(collector)
        monkeypatch.undo()
        importlib.reload(main).db_conn.close()
