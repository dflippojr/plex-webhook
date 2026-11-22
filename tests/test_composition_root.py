"""The composition root (#185): importing the app does nothing; create_app's lifespan builds the service."""
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from conftest import ADMIN_TOKEN, payload, post

ROOT = Path(__file__).resolve().parent.parent

# Imports third-party modules first, then records every file open, SQLite connect and
# directory creation while `app.main` is imported. Reading module source is not counted.
SCRIPT = """
import json, sys
import fastapi, prometheus_client, starlette, yaml  # noqa: F401  (their own imports are not the app's)
seen = []
def hook(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)) and not str(args[0]).endswith((".py", ".pyc")):
        seen.append([event, str(args[0])])
    elif event in ("sqlite3.connect", "os.mkdir"):
        seen.append([event, str(args[0])])
sys.addaudithook(hook)
import app.main  # noqa: F401
print(json.dumps(seen))
"""


def test_importing_the_app_opens_no_file_and_creates_no_directory(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)  # unwritable: startup could not create data/ here
    try:
        env = {**os.environ, "PYTHONPATH": str(ROOT), "DB_PATH": str(locked / "data" / "plex_events.db"),
               "ROOMS_CONFIG_PATH": str(locked / "rooms.yaml"),
               "DEVICES_SECRETS_PATH": str(locked / "devices.secrets.yaml")}
        out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, cwd=tmp_path, capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip() == "[]"
        assert list(locked.iterdir()) == []
    finally:
        locked.chmod(stat.S_IRWXU)


def test_starting_the_app_opens_the_store_and_stopping_closes_it(settings, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    app = create_app(settings)
    assert not settings.db_path.exists()
    with TestClient(app) as client:
        service = app.state.service
        assert settings.db_path.exists() and service.conn is not None
        assert client.get("/rooms", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).json()["rooms"]
    assert service.conn is None


def test_two_apps_on_one_database_keep_separate_state(settings, light_calls):
    first, second = create_app(settings), create_app(settings)
    with TestClient(first) as one, TestClient(second) as two:
        assert post(one, payload("media.play")).status_code == 200
        one.app.state.service.queue.drain(timeout=30)
        assert first.state.service.playback.active_clients["living_room"]
        assert second.state.service.playback.active_clients == {}
        assert first.state.service.metrics is not second.state.service.metrics
        events = 'plex_webhook_events_total{account="known",event="media.play",player="Living Room TV"}'
        assert f"{events} 1.0" in one.get("/metrics").text
        assert events not in two.get("/metrics").text


def test_unreadable_restore_records_leave_no_pending_rooms_and_startup_continues(playback, monkeypatch, caplog):
    def unavailable():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(playback.records, "rooms_with_restore_records", unavailable)
    playback.load_pending_rooms()
    assert playback.pending_rooms == set()
    assert "reason=restore_records_unavailable operation=load_pending" in caplog.text


def test_stopping_a_service_twice_is_harmless(make_service):
    service = make_service()
    service.stop()
    service.stop()
    assert service.conn is None
