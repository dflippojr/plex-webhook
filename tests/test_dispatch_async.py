import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import db, dispatcher, lights, main

ROOM = "den"
CLIENT_UUID = "client-1"


@pytest.fixture(autouse=True)
def fake_room(monkeypatch, tmp_path):
    # TestClient serves requests on its own thread; production uses one event-loop thread.
    conn = sqlite3.connect(tmp_path / "events.db", check_same_thread=False)
    conn.executescript(db.SCHEMA)
    monkeypatch.setattr(main, "db_conn", conn)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "EVENT_LOG", tmp_path / "events.jsonl")
    monkeypatch.setattr(dispatcher.registry, "resolve_room", lambda payload: ROOM)
    monkeypatch.setattr(dispatcher.registry, "lights_for", lambda room_key: [{"brand": "fake"}])
    dispatcher._active_clients.clear()
    yield
    dispatcher._action_executor.submit(lambda: None).result(timeout=30)
    dispatcher._active_clients.clear()


def post_event(client, event):
    payload = {"event": event, "Player": {"uuid": CLIENT_UUID, "title": "TV"}}
    return client.post("/webhook", data={"payload": json.dumps(payload)})


def drain():
    dispatcher._action_executor.submit(lambda: None).result(timeout=30)


def test_slow_action_does_not_delay_health(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_apply(action, room_lights):
        started.set()
        release.wait(timeout=30)

    monkeypatch.setattr(lights, "apply_action", slow_apply)
    client = TestClient(main.app)
    try:
        t0 = time.monotonic()
        response = post_event(client, "media.play")
        assert response.json() == {"status": "received", "event": "media.play"}
        assert started.wait(timeout=5)
        assert client.get("/health").json() == {"status": "ok"}
        assert time.monotonic() - t0 < 2
    finally:
        release.set()
        drain()


def test_events_for_one_room_apply_in_order(monkeypatch):
    applied = []

    def record(action, room_lights):
        if action == "dim":
            time.sleep(0.3)  # a slow dim must not be overtaken by the restore
        applied.append(action)

    monkeypatch.setattr(lights, "apply_action", record)
    client = TestClient(main.app)
    post_event(client, "media.play")
    post_event(client, "media.stop")
    drain()
    assert applied == ["dim", "restore"]


def test_controller_exception_does_not_break_webhook_or_later_actions(monkeypatch):
    applied = []

    def flaky(action, room_lights):
        if action == "dim":
            raise RuntimeError("bulb unreachable")
        applied.append(action)

    monkeypatch.setattr(lights, "apply_action", flaky)
    client = TestClient(main.app)
    assert post_event(client, "media.play").status_code == 200
    assert post_event(client, "media.stop").status_code == 200
    drain()
    assert applied == ["restore"]


def test_success_counter_increments_after_action_applied(monkeypatch):
    sample = {"room": ROOM, "action": "dim"}
    before = dispatcher.DISPATCH_ACTIONS_TOTAL.labels(**sample)._value.get()
    monkeypatch.setattr(lights, "apply_action", lambda action, room_lights: None)
    post_event(TestClient(main.app), "media.play")
    drain()
    assert dispatcher.DISPATCH_ACTIONS_TOTAL.labels(**sample)._value.get() == before + 1
