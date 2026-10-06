import importlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from conftest import payload


@pytest.fixture
def client(tmp_path, monkeypatch, registry, light_calls):
    import app.db
    import app.main as main

    # TestClient runs handlers in another thread, so use a cross-thread connection.
    conn = sqlite3.connect(tmp_path / "events.db", check_same_thread=False)
    conn.executescript(app.db.SCHEMA)
    monkeypatch.setattr(main, "db_conn", conn)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "EVENT_LOG", tmp_path / "events.jsonl")
    with TestClient(main.app) as c:
        c.conn, c.log = conn, tmp_path / "events.jsonl"
        yield c
    conn.close()


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
    assert light_calls == [("dim", ["AA:BB"])]


def test_webhook_invalid_json_still_200_and_recorded(client, light_calls):
    resp = client.post("/webhook", data={"payload": "{not json"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "received", "event": None}
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
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
