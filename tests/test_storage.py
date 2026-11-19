"""The SQLite file under concurrent readers, versioned migrations and WAL (#180)."""
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app import audit, audit_cli, db
from conftest import payload, records

REPO = Path(__file__).resolve().parent.parent

# The tables and indexes master created before #180, frozen here so later schema
# edits cannot silently change what the upgrade tests start from.
MASTER_SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    event TEXT,
    account_title TEXT,
    player_title TEXT,
    player_uuid TEXT,
    media_type TEXT,
    title TEXT,
    grandparent_title TEXT,
    rating_key TEXT,
    raw_payload TEXT NOT NULL,
    raw_truncated INTEGER NOT NULL DEFAULT 0,
    attachments TEXT
);
CREATE TABLE light_restore_records (
    room TEXT NOT NULL,
    light_id TEXT NOT NULL,
    was_off INTEGER NOT NULL CHECK(was_off IN (0, 1)),
    restore_percent INTEGER,
    dim_percent INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (room, light_id)
);
CREATE TABLE automation_room_owners (
    target_room TEXT PRIMARY KEY,
    trigger_room TEXT NOT NULL
);
CREATE INDEX idx_events_event ON events(event);
CREATE INDEX idx_events_received_at ON events(received_at);
CREATE INDEX idx_events_player_labels_cover ON events(event, player_title, player_uuid, account_title);
CREATE INDEX idx_events_clients_cover ON events(player_title, player_uuid, received_at)
    WHERE player_title IS NOT NULL;
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_id TEXT,
    actor_verified INTEGER NOT NULL CHECK(actor_verified IN (0, 1)),
    action TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    changed_fields TEXT NOT NULL,
    checksum TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX idx_audit_time ON audit_events(recorded_at);
CREATE INDEX idx_audit_correlation ON audit_events(correlation_id, id);
CREATE INDEX idx_audit_action ON audit_events(action, id);
"""

# The same database as an earlier release left it: no events.raw_truncated or
# events.attachments, no audit_events.detail, and the replaced index names.
OLD_SCHEMA = (
    MASTER_SCHEMA
    .replace("    raw_truncated INTEGER NOT NULL DEFAULT 0,\n    attachments TEXT\n", "")
    .replace("raw_payload TEXT NOT NULL,\n", "raw_payload TEXT NOT NULL\n")
    .replace("    checksum TEXT NOT NULL,\n    detail TEXT\n", "    checksum TEXT NOT NULL\n")
    .replace("idx_events_player_labels_cover ON events(event, player_title, player_uuid, account_title)",
             "idx_events_labels_cover ON events(event, player_title, account_title)")
    .replace("CREATE INDEX idx_events_clients_cover ON events(player_title, player_uuid, received_at)\n"
             "    WHERE player_title IS NOT NULL;",
             "CREATE INDEX idx_events_counts_cover ON events(COALESCE(NULLIF(event, ''), 'unknown'));")
)

EVENT_COLUMNS = ("id", "received_at", "event", "account_title", "player_title", "player_uuid", "media_type",
                 "title", "grandparent_title", "rating_key", "raw_payload")
AUDIT_COLUMNS = ("id", *audit.RECORD_COLUMNS)
DDL = re.compile(r"^\s*(CREATE|ALTER|DROP)\b|PRAGMA\s+(table_info|user_version|journal_mode\s*=)", re.IGNORECASE)


def receipt(path, **kwargs):
    fields = dict(action="webhook.receipt", outcome="received", reason_code="accepted",
                  correlation=audit.correlation_id())
    fields.update(kwargs)
    return audit.append(path, **fields)


@pytest.fixture
def started(tmp_path, monkeypatch):
    """A database opened the way service startup opens it, with the service connection kept open."""
    path = tmp_path / "data" / "plex_events.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.get_connection()
    yield path, conn
    conn.close()


def test_partly_read_cli_cursor_does_not_block_audit_or_event_writes(started):
    path, service = started
    for _ in range(3):
        receipt(path)
    reader = audit_cli.open_database(path)
    try:
        cursor = reader.execute("SELECT * FROM audit_events ORDER BY id")
        assert cursor.fetchone()["id"] == 1  # paused mid-stream, like `list` piped to a pager

        audit_id = receipt(path)
        event_id = db.insert_event(service, audit.utc_now(), "media.play", payload("media.play"), "{}")
        writer = db.get_connection()
        try:
            second_event_id = db.insert_event(writer, audit.utc_now(), "media.stop", payload("media.stop"), "{}")
        finally:
            writer.close()

        assert (audit_id, event_id, second_event_id) == (4, 1, 2)
        assert [row["id"] for row in cursor.fetchall()] == [2, 3]  # the reader keeps its snapshot
    finally:
        reader.close()
    assert [row["id"] for row in records(path)] == [1, 2, 3, 4]


def _legacy_database(path, schema):
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO events (received_at, event, player_title, raw_payload) "
                     "VALUES ('2026-01-01T00:00:00+00:00', 'media.play', 'Synthetic TV', '{\"k\":1}')")
        conn.execute("INSERT INTO events (received_at, raw_payload) VALUES ('2026-01-02T00:00:00+00:00', 'null')")
        conn.execute("INSERT INTO light_restore_records VALUES ('room_a', 'light-1', 0, 80, 20, 't')")
        conn.execute("INSERT INTO automation_room_owners VALUES ('room_b', 'room_a')")
        record = dict(recorded_at="2026-01-01T00:00:00.000000+00:00", correlation_id=audit.correlation_id(),
                      source="service", actor_kind="system", actor_id=None, actor_verified=0,
                      action="service.config_load", target_kind="rooms", target_id=None, outcome="activated",
                      reason_code="loaded", changed_fields={})
        record["checksum"] = audit.checksum(record)
        record["changed_fields"] = "{}"
        conn.execute(f"INSERT INTO audit_events ({', '.join(audit.RECORD_COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(audit.RECORD_COLUMNS))})",
                     tuple(record[key] for key in audit.RECORD_COLUMNS))
    conn.close()


def _snapshot(conn):
    return {
        "events": conn.execute(f"SELECT {', '.join(EVENT_COLUMNS)} FROM events ORDER BY id").fetchall(),
        "audit": conn.execute(f"SELECT {', '.join(AUDIT_COLUMNS)} FROM audit_events ORDER BY id").fetchall(),
        "restore": conn.execute("SELECT * FROM light_restore_records").fetchall(),
        "owners": conn.execute("SELECT * FROM automation_room_owners").fetchall(),
    }


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize("schema", [MASTER_SCHEMA, OLD_SCHEMA], ids=["master", "before-later-columns"])
def test_master_database_upgrades_to_latest_version_with_rows_intact(tmp_path, monkeypatch, schema):
    from app import storage

    path = tmp_path / "legacy.db"
    _legacy_database(path, schema)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        before = _snapshot(conn)
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    for _ in range(2):  # a second startup finds nothing left to do
        conn = db.get_connection()
        try:
            assert conn.execute("PRAGMA user_version").fetchone() == (len(storage.MIGRATIONS),)
            assert _snapshot(conn) == before
            assert {"raw_truncated", "attachments"} <= _columns(conn, "events")
            assert "detail" in _columns(conn, "audit_events")
            # Rows from before a column existed: not truncated, attachments unknown.
            assert conn.execute("SELECT raw_truncated, attachments FROM events").fetchall() == [(0, None), (0, None)]
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
            assert indexes == {"idx_events_event", "idx_events_received_at", "idx_events_player_labels_cover",
                               "idx_events_clients_cover"}
            assert {row[1] for row in conn.execute("PRAGMA index_list(audit_events)")} == {
                "idx_audit_time", "idx_audit_correlation", "idx_audit_action"}
        finally:
            conn.close()
    exported = records(path)
    assert len(exported) == 1 and exported[0]["checksum"] == audit.checksum(exported[0])
    assert "detail" not in exported[0]
    assert db.get_restore_record("room_a", "light-1") == {"was_off": False, "restore_percent": 80, "dim_percent": 20}
    assert db.automation_owner("room_b") == "room_a"


def test_append_and_restore_helpers_run_no_ddl(started, monkeypatch):
    path, _ = started
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    receipt(path)
    db.put_restore_record("room_a", "light-1", False, 80, 20)
    assert db.get_restore_record("room_a", "light-1")["dim_percent"] == 20
    assert db.rooms_with_restore_records() == {"room_a"}
    db.delete_restore_record("room_a", "light-1")
    db.set_automation_owner("room_b", "room_a")
    assert db.automation_owner("room_b") == "room_a"
    assert db.automation_targets("room_a") == {"room_b"}
    assert db.automation_sources() == {"room_a"}
    db.set_automation_owner("room_b", None)

    assert any("INSERT INTO audit_events" in statement for statement in statements)
    assert any("light_restore_records" in statement for statement in statements)
    assert [statement for statement in statements if DDL.search(statement)] == []


def test_journal_mode_is_wal_after_startup():
    import app.main  # noqa: F401  (startup opens DB_PATH)

    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    conn.close()


def _cli(path, *args):
    return subprocess.run([sys.executable, "-m", "app.audit_cli", "--db", str(path), *args],
                          cwd=REPO, capture_output=True, text=True, timeout=60)


def test_read_only_cli_reads_wal_database_through_sidecars_it_cannot_write(started):
    path, service = started
    receipt(path)
    receipt(path)
    db.insert_event(service, audit.utc_now(), "media.play", payload("media.play"), "{}")
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]
    assert all(sidecar.exists() for sidecar in sidecars)  # the running service keeps them

    # Stand-in for files owned by the service's uid: this process may read but not write
    # the database, its sidecars or their directory.
    for item in (path, *sidecars):
        item.chmod(0o444)
    path.parent.chmod(0o555)
    try:
        if os.geteuid() != 0:
            assert not any(os.access(item, os.W_OK) for item in (path.parent, path, *sidecars))
        listed = _cli(path, "list")
        assert listed.returncode == 0, listed.stderr
        assert len(listed.stdout.splitlines()) == 2
        exported = _cli(path, "export")
        assert [json.loads(line)["action"] for line in exported.stdout.splitlines()] == [
            "webhook.receipt", "webhook.receipt"]
    finally:
        path.parent.chmod(0o755)
        for item in (path, *sidecars):
            item.chmod(0o644)


def test_read_only_cli_without_sidecars_needs_a_writable_directory(started):
    """The documented limit: with the service stopped, the sidecars are gone and a reader must create -shm."""
    path, service = started
    receipt(path)
    service.close()  # the last connection to close removes -wal and -shm
    assert not path.with_name(path.name + "-shm").exists()
    path.chmod(0o444)
    path.parent.chmod(0o555)
    try:
        if os.geteuid() != 0:
            denied = _cli(path, "list")
            assert denied.returncode == 1
            assert denied.stderr == "audit_cli_failed: existing readable audit database required\n"
        # What running as the service's uid gives the reader; sidecars it creates get the file's mode.
        path.parent.chmod(0o755)
        path.chmod(0o644)
        assert _cli(path, "list").returncode == 0
    finally:
        path.parent.chmod(0o755)
        path.chmod(0o644)


def test_startup_keeps_working_when_the_file_cannot_switch_to_wal(tmp_path, monkeypatch, caplog):
    from app import storage

    path = tmp_path / "rollback.db"
    with sqlite3.connect(path) as conn:
        storage.migrate(conn)
    conn.close()
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM events").fetchone()  # a rollback-mode reader blocks the switch
    monkeypatch.setattr(storage, "BUSY_TIMEOUT_S", 0.05)
    try:
        conn = storage.open_database(path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            assert conn.execute("PRAGMA user_version").fetchone() == (len(storage.MIGRATIONS),)
            assert "reason=wal_unavailable" in caplog.text
        finally:
            conn.close()
    finally:
        reader.close()
    caplog.clear()
    conn = storage.open_database(path)  # the next start, with the reader gone, switches
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert "wal_unavailable" not in caplog.text
    finally:
        conn.close()


def _hold_read_lock(path):
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM events").fetchone()  # a rollback-mode reader, like an open CLI
    return reader


def test_first_upgrade_starts_on_the_existing_schema_while_a_reader_holds_the_file(tmp_path, monkeypatch, caplog):
    from app import storage

    path = tmp_path / "legacy.db"
    _legacy_database(path, MASTER_SCHEMA)  # version 0, rollback journal: the first start after #180
    with sqlite3.connect(path) as conn:
        before = _snapshot(conn)
    conn.close()
    monkeypatch.setattr(storage, "BUSY_TIMEOUT_S", 0.05)
    reader = _hold_read_lock(path)
    try:
        conn = storage.open_database(path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            assert conn.execute("PRAGMA user_version").fetchone() == (0,)
            assert _snapshot(conn) == before
            assert "reason=wal_unavailable" in caplog.text
            assert "reason=migration_deferred version=0" in caplog.text
        finally:
            conn.close()
    finally:
        reader.close()
    caplog.clear()
    conn = storage.open_database(path)  # the next start, with the reader gone, finishes the upgrade
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert conn.execute("PRAGMA user_version").fetchone() == (len(storage.MIGRATIONS),)
        assert _snapshot(conn) == before
        assert "migration_deferred" not in caplog.text
    finally:
        conn.close()


def _pragmas(conn):
    return conn.execute("PRAGMA journal_mode").fetchone()[0], conn.execute("PRAGMA synchronous").fetchone()[0]


def test_sync_stays_full_unless_the_file_is_in_wal_on_startup_and_later_connections(tmp_path, monkeypatch):
    from app import storage

    path = tmp_path / "data" / "plex_events.db"
    path.parent.mkdir()
    _legacy_database(path, MASTER_SCHEMA)
    monkeypatch.setattr(storage, "BUSY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(db, "DB_PATH", path)
    later = []
    connect = storage.connect

    def recording_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        later.append(_pragmas(conn))
        return conn

    def open_later_connections():
        later.clear()
        with monkeypatch.context() as patch:
            patch.setattr(storage, "connect", recording_connect)
            receipt(path)  # the audit append's own connection
            assert db.get_restore_record("room_a", "light-1") is not None  # the restore helper's connection
        return later

    reader = _hold_read_lock(path)
    try:
        service = db.get_connection()  # the reader keeps the file in rollback-journal mode
    finally:
        reader.close()
    try:
        # SQLite only keeps a file consistent under NORMAL in WAL mode: DELETE mode needs FULL (2).
        assert _pragmas(service) == ("delete", 2)
        assert open_later_connections() == [("delete", 2), ("delete", 2)]
    finally:
        service.close()

    service = db.get_connection()  # the next start switches to WAL, where NORMAL (1) is safe
    try:
        assert _pragmas(service) == ("wal", 1)
        assert open_later_connections() == [("wal", 1), ("wal", 1)]
    finally:
        service.close()


def test_first_upgrade_that_must_add_a_column_fails_while_a_reader_holds_the_file(tmp_path, monkeypatch):
    from app import storage

    path = tmp_path / "old.db"
    _legacy_database(path, OLD_SCHEMA)  # missing columns master added at start, which needs the write lock
    monkeypatch.setattr(storage, "BUSY_TIMEOUT_S", 0.05)
    reader = _hold_read_lock(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            storage.open_database(path).close()
    finally:
        reader.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        assert "raw_truncated" not in _columns(conn, "events")
    conn.close()


def test_a_failing_migration_step_leaves_the_file_at_the_previous_version(tmp_path, monkeypatch):
    from app import storage

    def broken(conn):
        conn.execute("ALTER TABLE events ADD COLUMN half_done TEXT")
        raise RuntimeError("step failed")

    path = tmp_path / "partial.db"
    with monkeypatch.context() as patch, pytest.raises(RuntimeError):
        patch.setattr(storage, "MIGRATIONS", (*storage.MIGRATIONS, broken))
        storage.open_database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (len(storage.MIGRATIONS),)
        assert "half_done" not in _columns(conn, "events")
    conn.close()


def test_a_file_from_newer_code_is_left_at_its_version(tmp_path):
    from app import storage

    path = tmp_path / "newer.db"
    storage.open_database(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {len(storage.MIGRATIONS) + 5}")
    conn.close()
    conn = storage.open_database(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (len(storage.MIGRATIONS) + 5,)
    finally:
        conn.close()


def test_a_step_another_process_finished_meanwhile_is_not_run_again(tmp_path):
    from app import storage

    path = tmp_path / "race.db"
    sqlite3.connect(path).close()

    class Racing(sqlite3.Connection):
        """Another process migrates the file between this connection's version check and its write lock."""
        raced = False

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE" and not Racing.raced:
                Racing.raced = True
                other = sqlite3.connect(path)
                storage.migrate(other)
                other.close()
            return super().execute(sql, *args)

    statements = []
    conn = sqlite3.connect(path, factory=Racing)
    conn.set_trace_callback(statements.append)
    try:
        storage.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone() == (len(storage.MIGRATIONS),)
    finally:
        conn.close()
    assert Racing.raced
    assert [statement for statement in statements if DDL.search(statement) and "user_version" not in statement] == []
