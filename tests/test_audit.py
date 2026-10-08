import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app import audit, audit_cli, db


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


def records(path):
    with audit_cli.open_database(path) as conn:
        args = audit_cli.parser().parse_args(["--db", str(path), "export", "--limit", "10000"])
        return list(audit_cli.read_records(conn, args))


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
        assert {"idx_events_counts_cover", "idx_events_clients_cover"} <= indexes


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
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


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
