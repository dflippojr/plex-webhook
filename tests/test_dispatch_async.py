"""HTTP stays responsive on one application loop while synthetic light work is held."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import db, lights, main
from conftest import drain

ROOM = "den"
CLIENT_UUID = "client-1"


@pytest.fixture
def fake_room(monkeypatch, tmp_path, registry, rooms_file, light_calls):
    rooms_file.write_text("""
rooms:
  den:
    plex_clients: [{title: Living Room TV, uuid: client-1}]
    lights: [{brand: govee, id: synthetic-light}]
""", encoding="utf-8")
    registry.reload()
    path = tmp_path / "events.db"
    # The HTTP loop and the light worker own separate connections to this database.
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        conn.executescript(db.SCHEMA)
        monkeypatch.setattr(db, "DB_PATH", path)
        monkeypatch.setattr(main, "db_conn", conn)
        monkeypatch.setattr(main, "DATA_DIR", tmp_path)
        monkeypatch.setattr(main, "EVENT_LOG", tmp_path / "events.jsonl")
        yield
    finally:
        try:
            drain()
        finally:
            conn.close()


def post_event(client, event):
    payload = {"event": event, "Player": {"uuid": CLIENT_UUID, "title": "Living Room TV"}}
    return client.post("/webhook", data={"payload": json.dumps(payload)})


def completed(action):
    return REGISTRY.get_sample_value("plex_dispatcher_actions_total", {"room": ROOM, "action": action}) or 0


def test_running_dim_keeps_http_responsive_and_restores_in_order(fake_room, monkeypatch):
    started, release = threading.Event(), threading.Event()
    applied = []

    def held_dim(action, room_lights, brightness=None, turn_on=True):
        if action == "dim":
            started.set()
            assert release.wait(timeout=30)
        applied.append(action)

    monkeypatch.setattr(lights, "apply_action", held_dim)
    before = {action: completed(action) for action in ("dim", "restore")}
    # Every request shares this portal and application loop. Helper futures let a
    # synchronous-dispatch regression time out here and still release the backend.
    with TestClient(main.app) as client, ThreadPoolExecutor(max_workers=2) as requests:
        pending = []

        def request(fn, *args):
            future = requests.submit(fn, *args)
            pending.append(future)
            return future

        try:
            play = request(post_event, client, "media.play")
            assert started.wait(timeout=5)
            response = play.result(timeout=5)
            assert response.status_code == 200
            assert response.json() == {"status": "received", "event": "media.play"}
            assert not release.is_set()

            health = request(client.get, "/health").result(timeout=5)
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert not release.is_set()

            metrics = request(client.get, "/metrics").result(timeout=5)
            assert metrics.status_code == 200
            assert "plex_dispatcher_actions_total" in metrics.text
            assert not release.is_set()

            stop = request(post_event, client, "media.stop").result(timeout=5)
            assert stop.status_code == 200
            assert stop.json() == {"status": "received", "event": "media.stop"}
            assert not release.is_set()
            assert applied == []
            assert {action: completed(action) for action in before} == before
        finally:
            release.set()
            try:
                for future in pending:
                    future.result(timeout=30)
            finally:
                drain()

    assert applied == ["dim", "restore"]
    assert {action: completed(action) - before[action] for action in before} == {"dim": 1, "restore": 1}
