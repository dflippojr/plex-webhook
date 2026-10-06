import sqlite3

from app import db
from app.db import event_counts, insert_event, last_received_at, list_known_clients
from conftest import payload


def test_get_connection_creates_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "sub" / "e.db")
    conn = db.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
    finally:
        conn.close()


def test_empty_db(memory_db):
    assert last_received_at(memory_db) is None
    assert event_counts(memory_db) == []
    assert list_known_clients(memory_db) == []


def test_insert_handles_missing_payload(memory_db):
    insert_event(memory_db, "2024-01-01T00:00:00+00:00", None, None, "null")
    assert event_counts(memory_db) == [("unknown", "unknown", "unknown", 1)]
    assert list_known_clients(memory_db) == []


def test_counts_group_by_event_player_account(memory_db):
    for event in ("media.play", "media.play", "media.stop"):
        insert_event(memory_db, "2024-01-01T00:00:00+00:00", event, payload(event), "{}")
    assert sorted(event_counts(memory_db)) == [
        ("media.play", "Living Room TV", "dan", 2),
        ("media.stop", "Living Room TV", "dan", 1),
    ]
    assert last_received_at(memory_db) == "2024-01-01T00:00:00+00:00"
