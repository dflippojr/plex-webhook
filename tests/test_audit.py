import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app import audit, audit_cli, db
from conftest import records


@pytest.fixture
def audit_db(tmp_path):
    path = tmp_path / "fixture # &.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.SCHEMA)
        db.insert_event(conn, "2026-01-01T00:00:00+00:00", "media.play", {}, '"SECRET"')
    return path


def receipt(path, **kwargs):
    fields = dict(action="webhook.receipt", outcome="received", reason_code="accepted",
                  correlation=audit.correlation_id())
    fields.update(kwargs)
    return audit.append(path, **fields)


def test_migration_reopen_checksums_and_legacy_preserved(audit_db):
    for _ in range(2):
        receipt(audit_db, event_id=1)
    rows = records(audit_db)
    assert [row["id"] for row in rows] == [1, 2]
    assert all(row["checksum"] == audit.checksum(row) for row in rows)
    assert all(row["target_id"] == "1" and row["target_kind"] == "event" for row in rows)
    assert all(datetime.fromisoformat(row["recorded_at"]).utcoffset() == timedelta(0) for row in rows)
    with sqlite3.connect(audit_db) as conn:
        conn.executescript(db.SCHEMA + audit.SCHEMA)
        conn.executescript(db.SCHEMA + audit.SCHEMA)
        assert db.event_counts(conn) == [("media.play", "unknown", "unknown", 1)]
        assert conn.execute("SELECT raw_payload FROM events").fetchone()[0] == '"SECRET"'
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
        assert {"idx_events_player_labels_cover", "idx_events_clients_cover"} <= indexes


@pytest.mark.parametrize("fields", [
    {"reason_code": "SECRET"}, {"correlation": "SECRET"}, {"action": "SECRET"},
    {"outcome": "SECRET"}, {"event_id": "SECRET"}, {"event_id": True},
    {"changed_fields": {"rooms.SECRET": 1}}, {"changed_fields": {"rooms": "SECRET"}},
    {"changed_fields": {"rooms": True}}, {"changed_fields": {"rooms": -1}},
    {"changed_fields": {"rooms": 1}},
])
def test_append_contract_rejects_untrusted_metadata(audit_db, fields):
    with pytest.raises((ValueError, KeyError)):
        receipt(audit_db, **fields)
    with sqlite3.connect(audit_db) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='audit_events'").fetchone() is None


def test_concurrent_owned_writers_and_legacy_receipts(audit_db):
    receipt(audit_db)
    def writer(index):
        if index % 2:
            with sqlite3.connect(audit_db) as conn:
                db.insert_event(conn, audit.utc_now(), "media.stop", {}, "{}")
        return receipt(audit_db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(writer, range(40)))
    assert len(set(ids)) == 40
    assert len(records(audit_db)) == 41
    with sqlite3.connect(audit_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 21


def test_cli_filters_pagination_readonly_and_export(audit_db, capsys):
    correlation = audit.correlation_id()
    receipt(audit_db, correlation=correlation)
    receipt(audit_db)
    rows = records(audit_db)
    before = audit_db.read_bytes()
    base = ["--db", str(audit_db), "export"]
    filters = [
        (["--id", "1"], [1]), (["--after-id", "1"], [2]),
        (["--limit", "1"], [1]), (["--correlation-id", correlation], [1]),
        (["--action", "rooms.reload"], []),
        (["--since", rows[1]["recorded_at"]], [2]),
        (["--until", rows[1]["recorded_at"]], [1]),
    ]
    for options, expected in filters:
        assert audit_cli.main(base + options) == 0
        output = capsys.readouterr().out
        assert "SECRET" not in output
        exported = [json.loads(line) for line in output.splitlines()]
        assert [row["id"] for row in exported] == expected
        assert all(set(row) == set(audit_cli.COLUMNS) for row in exported)
    assert audit_db.read_bytes() == before
    with audit_cli.open_database(audit_db) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM audit_events")
    assert audit_cli.main(["--db", str(audit_db), "list"]) == 0
    assert "webhook.receipt received accepted" in capsys.readouterr().out


@pytest.mark.parametrize("options", [["--limit", "0"], ["--limit", "10001"], ["--after-id", "-1"],
                                      ["--since", "2026-01-01"], ["--until", "SECRET"]])
def test_cli_rejects_bad_bounds(audit_db, options):
    with pytest.raises(SystemExit) as error:
        audit_cli.main(["--db", str(audit_db), "export"] + options)
    assert error.value.code == 2


def test_missing_db_never_created_or_exception_leaked(tmp_path, capsys):
    path = tmp_path / "SECRET.db"
    assert audit_cli.main(["--db", str(path), "export"]) == 1
    assert not path.exists()
    assert "SECRET" not in capsys.readouterr().err
    with pytest.raises(sqlite3.Error):
        receipt(path)
    assert not path.exists()


def test_reader_imports_no_service_or_devices(audit_db):
    code = "from app import audit_cli; import sys; assert not any(m in sys.modules for m in ('app.main', 'app.dispatcher', 'app.lights', 'app.rooms')); raise SystemExit(audit_cli.main(sys.argv[1:]))"
    receipt(audit_db)
    result = subprocess.run([sys.executable, "-c", code, "--db", str(audit_db), "export"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "SECRET" not in result.stdout


def test_retention_default_dry_run_boundary_and_monotonic_id(audit_db, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=90)
    monkeypatch.setattr(audit, "utc_now", lambda: (cutoff - timedelta(days=1)).isoformat(timespec="microseconds"))
    receipt(audit_db)
    monkeypatch.setattr(audit, "utc_now", lambda: cutoff.isoformat(timespec="microseconds"))
    receipt(audit_db)
    before = audit_db.read_bytes()
    with audit_cli.open_database(audit_db) as conn:
        result = audit_cli.prune(conn, now=now)
        assert result["eligible"] == 1 and result["deleted"] == 0
    assert audit_db.read_bytes() == before
    with audit_cli.open_database(audit_db, writable=True) as conn:
        assert audit_cli.prune(conn, apply=True, now=now)["deleted"] == 1
    assert [row["id"] for row in records(audit_db)] == [2]
    assert receipt(audit_db) == 3
    # Exercise the actual CLI, which keeps dry-run read-only.
    assert audit_cli.main(["--db", str(audit_db), "prune"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert audit_cli.main(["--db", str(audit_db), "prune", "--apply"]) == 0
    assert not json.loads(capsys.readouterr().out)["dry_run"]
    with sqlite3.connect(audit_db) as conn:
        # The fixture's 2026-01-01 event is past retention, so prune --apply now removes it.
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_sqlite_consistent_backup_restore_fixture(audit_db, tmp_path):
    receipt(audit_db)
    backup = tmp_path / "backup.db"
    with audit_cli.open_database(audit_db) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    saved = records(backup)
    receipt(audit_db)
    restored = tmp_path / "restored.db"
    with audit_cli.open_database(backup) as source, sqlite3.connect(restored) as destination:
        source.backup(destination)
    assert records(restored) == saved
    assert len(records(audit_db)) == 2 and len(records(restored)) == 1


def test_changes_omit_arbitrary_names_and_values():
    before = {"SECRET": {"plex_clients": [{"uuid": "SECRET"}], "lights": [], "SECRET": "SECRET"}}
    after = {"SECRET": {"plex_clients": [], "lights": [{"id": "SECRET"}], "SECRET": "changed"}}
    changes = audit.configuration_changes(before, after)
    assert changes == {"rooms.plex_clients": 1, "rooms.lights": 1, "rooms.other": 1}
    assert "SECRET" not in json.dumps(changes)
    assert audit.configuration_changes(before, before) == {}


def _event_retention_fixture(tmp_path):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=91)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    path = tmp_path / "ret.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.SCHEMA)
        db.insert_event(conn, old, "media.play", {}, '"old"')
        db.insert_event(conn, recent, "media.play", {}, '"new"')
    receipt(path)  # creates the audit table, as the running service does
    log = tmp_path / "events.jsonl"
    lines = [json.dumps({"received_at": old}), json.dumps({"received_at": recent}), "not json", json.dumps({"x": 1})]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, log, lines, now


def test_event_retention_dry_run_changes_nothing(tmp_path, capsys):
    path, log, _, _ = _event_retention_fixture(tmp_path)
    before_db, before_log = path.read_bytes(), log.read_bytes()
    assert audit_cli.main(["--db", str(path), "prune"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] and result["events_eligible"] == 1 and result["event_log_eligible"] == 1
    assert result["events_deleted"] == 0 and result["event_log_removed"] == 0
    assert path.read_bytes() == before_db and log.read_bytes() == before_log


def test_event_retention_apply_prunes_rows_and_trims_log(tmp_path, capsys):
    path, log, lines, _ = _event_retention_fixture(tmp_path)
    assert audit_cli.main(["--db", str(path), "prune", "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["events_deleted"] == 1 and result["event_log_removed"] == 1
    with sqlite3.connect(path) as conn:
        assert [r[0] for r in conn.execute("SELECT raw_payload FROM events")] == ['"new"']
    # Unparseable and timestamp-less lines are kept rather than guessed at.
    assert log.read_text(encoding="utf-8").splitlines() == lines[1:]
    assert not list(tmp_path.glob("*.tmp"))


def test_event_retention_explicit_log_path_and_missing_log(tmp_path, capsys):
    path, log, _, _ = _event_retention_fixture(tmp_path)
    other = tmp_path / "elsewhere.jsonl"
    log.rename(other)
    assert audit_cli.main(["--db", str(path), "prune", "--apply", "--events-log", str(other)]) == 0
    assert json.loads(capsys.readouterr().out)["event_log_removed"] == 1
    other.unlink()
    assert audit_cli.main(["--db", str(path), "prune"]) == 0
    assert json.loads(capsys.readouterr().out)["event_log_eligible"] == 0


def _boundary_prune(tmp_path, received_values, log_received):
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.SCHEMA + audit.SCHEMA)
        for received in received_values:
            db.insert_event(conn, received, "media.play", {"event": "media.play"}, "{}")
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps({"received_at": log_received, "event": "media.play"}) + "\n", encoding="utf-8")
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)  # cutoff = 2026-07-18T12:00:00.000000+00:00
    with audit_cli.open_database(path, writable=True) as conn:
        result = audit_cli.prune(conn, apply=True, now=now, events_log=log)
    with sqlite3.connect(path) as conn:
        kept = [r[0] for r in conn.execute("SELECT received_at FROM events ORDER BY id")]
    return result, kept


def test_prune_retains_events_received_exactly_on_cutoff_in_either_shape(tmp_path):
    shapes = ["2026-07-18T12:00:00+00:00", "2026-07-18T12:00:00.000000+00:00"]
    result, kept = _boundary_prune(tmp_path, shapes, shapes[0])
    assert result["events_eligible"] == result["events_deleted"] == 0
    assert result["event_log_removed"] == 0
    assert kept == shapes


def test_prune_deletes_events_one_microsecond_before_cutoff_in_either_shape(tmp_path):
    before = ["2026-07-18T11:59:59.999999+00:00", "2026-07-18T11:59:59+00:00"]
    result, kept = _boundary_prune(tmp_path, before + ["2026-07-18T12:00:00.000001+00:00"], before[0])
    assert result["events_deleted"] == 2 and result["event_log_removed"] == 1
    assert kept == ["2026-07-18T12:00:00.000001+00:00"]


def _audit_rows(path):
    with sqlite3.connect(path) as conn:
        if conn.execute("SELECT name FROM sqlite_master WHERE name='audit_events'").fetchone() is None:
            return []
        return conn.execute("SELECT action FROM audit_events").fetchall()


@pytest.mark.parametrize("call", [
    dict(action="rooms.reload", outcome="denied", reason_code="invalid_credential", admin=True),
    dict(action="rooms.reload", outcome="activated", reason_code="loaded", target_id="x"),
    dict(action="clients.read", outcome="denied", reason_code="missing_credential", target_id="x"),
])
def test_admin_denied_and_untargeted_ids_are_rejected(audit_db, call):
    with pytest.raises(ValueError):
        audit.append(audit_db, correlation=audit.correlation_id(), **call)
    assert _audit_rows(audit_db) == []


def test_room_target_id_is_stored_for_room_operations(audit_db):
    audit.append(audit_db, action="lights.check", outcome="completed", reason_code="read",
                 correlation=audit.correlation_id(), admin=True, target_id="den")
    [row] = records(audit_db)
    assert (row["target_kind"], row["target_id"], row["actor_id"]) == ("room", "den", "owner-admin")


def _queued(path, brand="govee", brightness=20):
    return audit.append(path, action="light.action_queued", outcome="queued", reason_code="accepted",
                        correlation=audit.correlation_id(), target_id="den",
                        detail={"action_id": audit.correlation_id(), "request": "dim", "on_behalf_of": "plex_server",
                                "targets": [{"brand": brand, "id": "light-1", "brightness": brightness}]})


@pytest.mark.parametrize("target", [{"brand": ""}, {"brand": 7}, {"brightness": 101}, {"brightness": -1},
                                    {"brightness": True}])
def test_light_detail_rejects_bad_labels_and_numbers(audit_db, target):
    with pytest.raises(ValueError):
        _queued(audit_db, **target)
    assert _audit_rows(audit_db) == []


def test_light_detail_accepts_brightness_bounds_and_none(audit_db):
    for brightness in (None, 0, 100):
        _queued(audit_db, brightness=brightness)
    assert [row["detail"]["targets"][0]["brightness"] for row in records(audit_db)] == [None, 0, 100]


def test_legacy_audit_table_gains_detail_column_and_keeps_checksums(audit_db, capsys):
    legacy_schema = audit.SCHEMA.replace("checksum TEXT NOT NULL,\n    detail TEXT", "checksum TEXT NOT NULL")
    assert legacy_schema != audit.SCHEMA
    # A receipt as a deployed service wrote it before light records: no detail key in the checksum.
    old = dict(recorded_at=audit.utc_now(), correlation_id=audit.correlation_id(), source="webhook",
               actor_kind="plex_server", actor_id="plex-server", actor_verified=0, action="webhook.receipt",
               target_kind="event", target_id="1", outcome="received", reason_code="accepted", changed_fields={})
    old["checksum"] = audit.checksum(old)
    old["changed_fields"] = "{}"
    with sqlite3.connect(audit_db) as conn:
        conn.executescript(legacy_schema)
        assert not audit.has_detail_column(conn)
        conn.execute(f"INSERT INTO audit_events ({', '.join(audit.RECORD_COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(audit.RECORD_COLUMNS))})",
                     tuple(old[key] for key in audit.RECORD_COLUMNS))
    audit.append(audit_db, action="light.result", outcome="command_sent", reason_code="lan_command_sent",
                 correlation=audit.correlation_id(), target_id="light-1",
                 detail={"action_id": audit.correlation_id(), "request": "dim", "on_behalf_of": "plex_server",
                         "brand": "govee", "transport": "lan", "credential_source": "none",
                         "progress": ["brightness"], "attempts": []})
    with sqlite3.connect(audit_db) as conn:
        assert audit.has_detail_column(conn)
    old, new = records(audit_db)
    assert old["checksum"] == audit.checksum(old) and new["checksum"] == audit.checksum(new)
    assert new["detail"]["brand"] == "govee" and new["detail"]["progress"] == ["brightness"]
    assert audit_cli.main(["--db", str(audit_db), "export"]) == 0
    exported = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["action"] for row in exported] == ["webhook.receipt", "light.result"]
    assert ["detail" in row for row in exported] == [False, True]


def test_export_reads_legacy_table_without_detail_column(audit_db, capsys):
    # A database last written before light records, opened read-only, is never migrated.
    legacy_schema = audit.SCHEMA.replace("checksum TEXT NOT NULL,\n    detail TEXT", "checksum TEXT NOT NULL")
    assert legacy_schema != audit.SCHEMA
    rows = []
    for action, outcome in (("webhook.receipt", "received"), ("webhook.receipt", "rejected")):
        row = dict(recorded_at=audit.utc_now(), correlation_id=audit.correlation_id(), source="webhook",
                   actor_kind="plex_server", actor_id="plex-server", actor_verified=0, action=action,
                   target_kind="event", target_id="1", outcome=outcome, reason_code="accepted", changed_fields={})
        row["checksum"] = audit.checksum(row)
        rows.append(row)
    with sqlite3.connect(audit_db) as conn:
        conn.executescript(legacy_schema)
        conn.executemany(f"INSERT INTO audit_events ({', '.join(audit.RECORD_COLUMNS)}) "
                         f"VALUES ({', '.join('?' * len(audit.RECORD_COLUMNS))})",
                         [tuple(json.dumps(row[key]) if key == "changed_fields" else row[key]
                                for key in audit.RECORD_COLUMNS) for row in rows])
    assert audit_cli.main(["--db", str(audit_db), "export"]) == 0
    exported = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["outcome"] for row in exported] == ["received", "rejected"]
    assert all(set(row) == set(audit_cli.COLUMNS) for row in exported)
    assert all(row["checksum"] == audit.checksum(row) for row in exported)
    with sqlite3.connect(audit_db) as conn:
        assert not audit.has_detail_column(conn)


def test_prune_keeps_log_lines_with_naive_timestamps(tmp_path):
    log = tmp_path / "events.jsonl"
    naive = json.dumps({"received_at": "2000-01-01T00:00:00", "event": "media.play"}) + "\n"
    old = json.dumps({"received_at": "2000-01-01T00:00:00+00:00", "event": "media.play"}) + "\n"
    log.write_text(naive + old, encoding="utf-8")
    cutoff = (datetime(2026, 10, 16, tzinfo=timezone.utc) - timedelta(days=90)).isoformat(timespec="microseconds")
    assert audit_cli.prune_event_log(log, cutoff) == (1, 1)
    assert audit_cli.prune_event_log(log, cutoff, apply=True) == (1, 1)
    assert log.read_text(encoding="utf-8") == naive


def test_failed_log_replace_leaves_log_intact_and_no_temp_file(tmp_path, monkeypatch, capsys):
    path, log, _, _ = _event_retention_fixture(tmp_path)
    before = log.read_bytes()

    def fail_replace(*_args):
        raise OSError("SECRET")

    monkeypatch.setattr(audit_cli.os, "replace", fail_replace)
    assert audit_cli.main(["--db", str(path), "prune", "--apply"]) == 1
    assert log.read_bytes() == before
    assert not (tmp_path / "events.jsonl.prune.tmp").exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "audit_cli_failed: existing readable audit database required\n"


def test_prune_without_events_table(tmp_path, monkeypatch):
    path = tmp_path / "audit-only.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(audit.SCHEMA)
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    for recorded in (now - timedelta(days=91), now - timedelta(days=1)):
        monkeypatch.setattr(audit, "utc_now", lambda value=recorded: value.isoformat(timespec="microseconds"))
        receipt(path)
    with audit_cli.open_database(path) as conn:
        result = audit_cli.prune(conn, now=now)
    assert result["eligible"] == 1 and result["deleted"] == 0
    assert result["events_eligible"] == result["events_deleted"] == 0
    with audit_cli.open_database(path, writable=True) as conn:
        result = audit_cli.prune(conn, apply=True, now=now)
    assert result["eligible"] == result["deleted"] == 1
    assert result["events_eligible"] == result["events_deleted"] == 0
    assert [row["recorded_at"] for row in records(path)] == [(now - timedelta(days=1)).isoformat(timespec="microseconds")]


def test_module_entry_point_returns_exit_status(tmp_path):
    path = tmp_path / "SECRET.db"
    result = subprocess.run([sys.executable, "-m", "app.audit_cli", "--db", str(path), "list"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stderr
    assert result.stderr == "audit_cli_failed: existing readable audit database required\n"
    assert not path.exists()
