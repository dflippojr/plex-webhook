"""Index migration, query equivalence and plans; never assert wall-clock timing."""

import sqlite3

import pytest

from app import db
from scripts.benchmark_sqlite_indexes import (
    INDEX_NAMES,
    QUERIES,
    drop_new_indexes,
    query_plan,
    seed_history,
)


def test_existing_history_survives_first_and_second_open(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    drop_new_indexes(conn)
    db.insert_event(conn, "2026-10-01", "media.play", {
        "Player": {"title": "TV", "uuid": "uuid"}, "Account": {"title": "user"},
        "Metadata": {"type": "movie", "title": "Film", "ratingKey": "42"},
    }, '{"original":"payload"}')
    history = conn.execute("SELECT * FROM events").fetchall()
    counters = db.event_counts(conn)
    clients = db.list_known_clients(conn)
    conn.close()

    for _ in range(2):
        conn = db.get_connection()
        try:
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
            assert indexes == {*INDEX_NAMES, "idx_events_event", "idx_events_received_at"}
            assert conn.execute("SELECT * FROM events").fetchall() == history
            assert db.event_counts(conn) == counters
            assert db.list_known_clients(conn) == clients
        finally:
            conn.close()


@pytest.mark.parametrize("populated", [False, True])
def test_indexed_queries_match_legacy_semantics(memory_db, populated):
    drop_new_indexes(memory_db)
    if populated:
        rows = [
            ("2026-10-01", None, None, None, None, "null"),
            ("2026-10-02", "", "", None, "", "{}"),
            ("2026-10-03", "unknown", "unknown", None, "unknown", "{}"),
            ("2026-10-04", "media.play", "Old TV", "stable", "alice", "{}"),
            ("2026-10-05", "media.play", "Renamed TV", "stable", "alice", "{}"),
            ("2026-10-06", "media.play", "Renamed TV", "stable", "bob", "{}"),
            ("2026-10-07", "media.stop", "Phone", None, "bob", "{}"),
            ("2026-10-08", "media.stop", "Phone", None, "bob", "{}"),
            ("2026-10-08", "media.pause", "Phone", "other", "bob", "{}"),
        ]
        memory_db.executemany(
            "INSERT INTO events "
            "(received_at,event,player_title,player_uuid,account_title,raw_payload) "
            "VALUES (?,?,?,?,?,?)", rows,
        )
        memory_db.commit()
    before_counts = db.event_counts(memory_db)
    before_clients = db.list_known_clients(memory_db)
    history = memory_db.execute("SELECT * FROM events").fetchall()
    memory_db.executescript(db.SCHEMA)
    assert sorted(db.event_counts(memory_db)) == sorted(before_counts)
    clients = db.list_known_clients(memory_db)
    assert sorted(clients, key=repr) == sorted(before_clients, key=repr)
    assert [row["last_seen"] for row in clients] == sorted(
        (row["last_seen"] for row in clients), reverse=True,
    )
    assert memory_db.execute("SELECT * FROM events").fetchall() == history
    if populated:
        assert ("unknown", "unknown", "unknown", 3) in db.event_counts(memory_db)
        assert len(clients) == 6  # NULL titles excluded; empty titles retained.
        assert next(row for row in clients if row["title"] == "Renamed TV")["event_count"] == 2
        assert next(row for row in clients if row["title"] == "Phone" and row["uuid"] is None)["event_count"] == 2
    else:
        assert db.event_counts(memory_db) == clients == []


def test_100k_queries_use_indexes_without_group_sort(tmp_path):
    conn = sqlite3.connect(tmp_path / "synthetic.db")
    try:
        conn.executescript(db.SCHEMA)
        drop_new_indexes(conn)
        seed_history(conn, 100000)
        baseline = {name: function(conn) for name, function in QUERIES.items()}
        conn.executescript(db.SCHEMA)
        for (name, function), index in zip(QUERIES.items(), INDEX_NAMES):
            key = repr if name == "list_known_clients" else None
            assert sorted(function(conn), key=key) == sorted(baseline[name], key=key)
            plan = query_plan(conn, function)
            assert any(index in step for step in plan), plan
            assert not any("TEMP B-TREE FOR GROUP BY" in step for step in plan), plan
            if name == "list_known_clients":
                assert any("COVERING INDEX" in step for step in plan), plan
        assert len(db.event_counts(conn)) == 36
        assert len(db.list_known_clients(conn)) == 12
    finally:
        conn.close()


def test_labeled_counts_fold_many_distinct_values_on_the_covering_index(tmp_path):
    conn = sqlite3.connect(tmp_path / "distinct.db")
    try:
        conn.executescript(db.SCHEMA)
        conn.executemany(
            "INSERT INTO events (received_at,event,player_title,account_title,raw_payload) VALUES (?,?,?,?,?)",
            (("2026-10-01", f"evt.{i}", f"Player {i}", f"user{i}" if i % 2 else None, "{}") for i in range(20000)),
        )
        conn.commit()

        def label(event, player, account):
            return ("other", "other", "other" if account == "unknown" else "known")

        assert db.labeled_event_counts(conn, label) == {("other", "other", "known"): 10000,
                                                        ("other", "other", "other"): 10000}
        plan = query_plan(conn, lambda c: db.labeled_event_counts(c, label))
        assert any("COVERING INDEX idx_events_labels_cover" in step for step in plan), plan
        assert not any("TEMP B-TREE" in step for step in plan), plan
    finally:
        conn.close()


def test_expression_index_from_earlier_releases_is_replaced(tmp_path, monkeypatch):
    path = tmp_path / "expression-index.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute("CREATE INDEX idx_events_counts_cover ON events("
                 "COALESCE(NULLIF(event, ''), 'unknown'), COALESCE(NULLIF(player_title, ''), 'unknown'), "
                 "COALESCE(NULLIF(account_title, ''), 'unknown'))")
    conn.close()
    conn = db.get_connection()
    try:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
        assert "idx_events_counts_cover" not in indexes
        assert "idx_events_labels_cover" in indexes
    finally:
        conn.close()
