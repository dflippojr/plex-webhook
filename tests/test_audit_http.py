import json
import importlib
import sqlite3

import pytest
from prometheus_client import REGISTRY

from app import audit
from conftest import drain, payload, post, records, rows


@pytest.mark.parametrize("body,code", [
    ("{SECRET", "invalid_json"), ("[]", "invalid_payload"),
    ('{"event": []}', "invalid_payload"), ('{"event": "media.play", "Player": []}', "invalid_payload"),
    (None, "missing_payload"),
])
def test_rejected_receipts_still_capture(client, body, code):
    response = client.post("/webhook", data={} if body is None else {"payload": body})
    assert response.status_code == 200
    record = rows(client)[0]
    assert record["action"] == "webhook.receipt"
    assert record["outcome"] == "rejected" and record["reason_code"] == code
    assert record["actor_kind"] == "plex_server" and record["actor_id"] == "plex-server"
    assert record["actor_verified"] == 0 and record["source"] == "webhook"
    assert record["target_kind"] == "event" and record["target_id"] == "1"
    if body is not None and code != "invalid_json":
        assert json.loads(client.conn.execute("SELECT raw_payload FROM events").fetchone()[0]) == json.loads(body)


def test_receipt_privacy_headers_payload_attachments_export(client, caplog):
    body = payload("media.play", title="SECRET", uuid="SECRET", account="SECRET")
    body["Metadata"]["title"] = "SECRET"
    body["token"] = "SECRET"
    response = client.post("/webhook", data={"payload": json.dumps(body)},
                           headers={"Authorization": "SECRET", "X-Actor": "SECRET"},
                           files={"SECRET": ("SECRET.jpg", b"SECRET", "image/jpeg")})
    assert response.status_code == 200
    record = rows(client)[0]
    assert record["outcome"] == "received" and record["reason_code"] == "accepted"
    assert "SECRET" not in json.dumps(record) and "SECRET" not in caplog.text
    assert record["checksum"] == audit.checksum(record)
    # Existing raw history remains intentionally separate.
    assert "SECRET" in client.conn.execute("SELECT raw_payload FROM events").fetchone()[0]


def test_reload_validation_results_and_changes(client, rooms_file, registry):
    rooms_file.write_text("rooms: {SECRET: {SECRET: SECRET, lights: [{brand: SECRET, id: SECRET}]}}", encoding="utf-8")
    assert client.post("/rooms/validate").status_code == 200
    assert client.post("/rooms/reload").status_code == 200
    assert client.post("/rooms/reload").status_code == 200
    validation, activated, unchanged = rows(client)
    assert validation["action"] == "rooms.validate" and validation["outcome"] == "validated"
    assert validation["changed_fields"] == {}
    assert activated["action"] == "rooms.reload" and activated["outcome"] == "activated"
    assert activated["changed_fields"]["rooms"] == 3
    assert unchanged["changed_fields"] == {}
    assert all(row["target_kind"] == "rooms" and row["target_id"] is None for row in rows(client))
    assert "SECRET" not in json.dumps(rows(client))
    before = registry._state
    rooms_file.write_text("rooms: {SECRET: {lights: [{id: SECRET}]}}", encoding="utf-8")
    assert client.post("/rooms/reload").status_code == 422
    assert registry._state is before
    assert rows(client)[-1]["reason_code"] == "invalid_config"
    rooms_file.unlink()
    assert client.post("/rooms/reload").status_code == 503
    assert rows(client)[-1]["reason_code"] == "config_unavailable"
    assert all(row["changed_fields"] == {} for row in rows(client)[-2:])
    assert registry._state is before


@pytest.mark.parametrize("endpoint", ["webhook", "rooms/reload", "rooms/validate"])
def test_audit_failure_does_not_block_or_retry(client, rooms_file, monkeypatch, light_calls, caplog, endpoint):
    import app.main as main
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise sqlite3.OperationalError("SECRET")
    monkeypatch.setattr(audit, "append", fail)
    metric = "plex_webhook_audit_write_failures_total"
    before = REGISTRY.get_sample_value(metric) or 0
    if endpoint == "webhook":
        response = post(client, payload("media.play", uuid="uuid-living"))
        drain()
        assert light_calls == [("dim", ["AA:BB"])]
    else:
        rooms_file.write_text("rooms: {SECRET: {}}", encoding="utf-8")
        response = client.post("/" + endpoint)
        if endpoint.endswith("reload"):
            assert list(main.registry.rooms) == ["SECRET"]
    assert response.status_code == 200
    # The webhook also tries its queued and summary records; each failure is counted, none retried.
    expected = 3 if endpoint == "webhook" else 1
    assert len(calls) == expected
    assert REGISTRY.get_sample_value(metric) == before + expected
    assert "reason=audit_write_failed" in caplog.text and "SECRET" not in caplog.text.split("reason=audit_write_failed")[-1]


def test_locked_database_fail_open_reload(client, rooms_file, caplog):
    path = client.conn.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as locked:
        locked.execute("BEGIN IMMEDIATE")
        rooms_file.write_text("rooms: {den: {}}", encoding="utf-8")
        assert client.post("/rooms/reload").status_code == 200
    assert "reason=audit_write_failed" in caplog.text


def test_bad_form_audited_safely(client, monkeypatch):
    from starlette.requests import Request
    async def fail_form(self):
        raise ValueError("SECRET")
    monkeypatch.setattr(Request, "form", fail_form)
    response = client.post("/webhook", data={"payload": "SECRET"})
    assert response.status_code == 400
    assert "SECRET" not in response.text
    assert rows(client)[0]["reason_code"] == "invalid_form"


@pytest.mark.parametrize("source,outcome,reason", [
    ("rooms: {SECRET: {}}", "activated", "loaded"),
    ("rooms: {SECRET: {lights: [{id: SECRET}]}}", "rejected", "invalid_config"),
    (None, "rejected", "config_unavailable"),
])
def test_startup_config_audit(tmp_path, monkeypatch, caplog, source, outcome, reason):
    import app.db as database
    import app.main as main
    import app.rooms as rooms
    config = tmp_path / "startup.yaml"
    if source is not None:
        config.write_text(source, encoding="utf-8")
    startup = rooms.RoomRegistry(config)
    path = tmp_path / "startup.db"
    def unregister():
        for collector in (main.EVENTS_TOTAL, main.LAST_EVENT_TIMESTAMP):
            REGISTRY.unregister(collector)
    unregister()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(database, "DB_PATH", path)
            patch.setattr(rooms, "registry", startup)
            importlib.reload(main).db_conn.close()
            record = records(path)[0]
            assert record["action"] == "service.config_load" and record["source"] == "service"
            assert record["actor_kind"] == "system" and record["actor_verified"] == 0
            assert record["outcome"] == outcome and record["reason_code"] == reason
            assert "SECRET" not in json.dumps(record) and "SECRET" not in caplog.text
            unregister()
    finally:
        importlib.reload(main).db_conn.close()


def test_valid_numeric_metadata_and_null_groups_keep_legacy_capture(client):
    body = {"event": "library.new", "Account": None, "Player": None,
            "Metadata": {"ratingKey": 42, "duration": 1000}}
    assert post(client, body).status_code == 200
    assert rows(client)[0]["outcome"] == "received"
    assert json.loads(client.conn.execute("SELECT raw_payload FROM events").fetchone()[0]) == body
