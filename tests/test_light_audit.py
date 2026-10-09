"""Light actions are correlated with their webhook receipt and report transport evidence only.

Everything here uses fake sockets, fake tinytuya/requests and temporary databases.
"""
import json
import logging
import sys
import threading
import types
import uuid

import pytest
import requests

from app import audit, audit_cli, dispatcher, lights
from conftest import drain, payload, post, records, rows

REAL_APPLY_ACTION = lights.apply_action
SENTINEL = "synthetic-secret-sentinel"

GOVEE = {"brand": "govee", "id": "AA:BB", "name": "Lamp", "model": "H6159"}
TUYA = {"brand": "tuya", "id": "plug1", "name": "Plug"}


@pytest.fixture
def real_lights(monkeypatch, light_calls):
    """Undo conftest's recorder so the real controllers run (against fakes below)."""
    monkeypatch.setattr(lights, "apply_action", REAL_APPLY_ACTION)
    monkeypatch.setattr(lights, "_secrets_cache", {})
    for name in ("GOVEE_API_KEY", "TUYA_ACCESS_ID", "TUYA_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def lan(monkeypatch):
    """Fake Govee LAN: record commands, optionally fail the Nth send, never touch a socket."""
    state = types.SimpleNamespace(sent=[], fail_on=None)

    def send(self, ip, message):
        if state.fail_on == len(state.sent):
            raise OSError(SENTINEL)
        state.sent.append(message["msg"]["cmd"])

    monkeypatch.setattr(lights.GoveeController, "_send_lan_command", send)
    monkeypatch.setattr(lights.GoveeController, "_discover_ip", lambda self, device_id: None)
    return state


def fake_tinytuya(monkeypatch, *, local=None, cloud=None, local_error=None, cloud_error=None):
    """local/cloud: iterables of returns (turn, brightness) consumed in order."""
    local, cloud = iter(local or ()), iter(cloud or ())

    class Device:
        def __init__(self, *args):
            if local_error:
                raise local_error

        set_version = set_socketTimeout = lambda self, value: None
        turn_on = lambda self: next(local)
        set_value = lambda self, dp, value: next(local)

    class Cloud:
        def __init__(self, **kwargs):
            if cloud_error:
                raise cloud_error

        sendcommand = lambda self, device, commands: next(cloud)

    monkeypatch.setitem(sys.modules, "tinytuya", types.SimpleNamespace(OutletDevice=Device, Cloud=Cloud))


def only(light):
    (result,) = REAL_APPLY_ACTION("dim", [light])
    return result


# --- evidence levels and fixed codes -------------------------------------------


def test_lan_success_is_command_sent_not_verified(real_lights, lan, monkeypatch):
    monkeypatch.setattr(lights, "_secrets_cache", {"AA:BB": {"ip": "192.0.2.5"}})
    result = only(GOVEE)
    assert (result.outcome, result.reason, result.transport) == ("command_sent", "lan_command_sent", "lan")
    assert result.credential_source == "device_config" and result.progress == ("turn", "brightness")
    assert lan.sent == ["turn", "brightness"]


def test_cloud_fallback_after_partial_lan(real_lights, lan, monkeypatch):
    monkeypatch.setattr(lights, "_secrets_cache", {"AA:BB": {"ip": "192.0.2.5"}})
    monkeypatch.setenv("GOVEE_API_KEY", SENTINEL)
    lan.fail_on = 1  # turn goes out, brightness fails
    monkeypatch.setattr(requests, "put", lambda *a, **k: types.SimpleNamespace(raise_for_status=lambda: None))
    result = only(GOVEE)
    assert (result.outcome, result.reason, result.transport) == (
        "request_accepted_by_transport", "cloud_accepted", "cloud")
    assert result.credential_source == "environment"
    lan_attempt, cloud_attempt = result.attempts
    assert (lan_attempt.outcome, lan_attempt.reason, lan_attempt.progress) == ("failed", "send_error", ("turn",))
    assert cloud_attempt.progress == ("turn", "brightness")


def test_partial_progress_survives_total_failure(real_lights, lan, monkeypatch):
    monkeypatch.setattr(lights, "_secrets_cache", {"AA:BB": {"ip": "192.0.2.5"}})
    lan.fail_on = 1
    result = only(GOVEE)
    assert (result.outcome, result.reason, result.progress) == ("failed", "send_error", ("turn",))


@pytest.mark.parametrize("light, env, expected", [
    (GOVEE, {}, ("skipped", "missing_credentials")),
    (GOVEE, {"GOVEE_API_KEY": SENTINEL}, ("skipped", "missing_model")),
    ({"brand": "hue", "id": "x"}, {}, ("skipped", "unsupported_brand")),
    ({"id": "x"}, {}, ("skipped", "unsupported_brand")),
    (TUYA, {}, ("skipped", "missing_credentials")),
])
def test_skip_codes(real_lights, lan, monkeypatch, light, env, expected):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    light = {**light, "model": None} if env == {"GOVEE_API_KEY": SENTINEL} else light
    result = only(light)
    assert (result.outcome, result.reason) == expected


def test_unexpected_exception_becomes_failure_and_later_lights_run(real_lights, lan, monkeypatch):
    monkeypatch.setattr(lights.GoveeController, "_try_lan", lambda *a: 1 / 0)
    results = REAL_APPLY_ACTION("dim", [GOVEE, {"brand": "hue", "id": "later"}])
    assert [(r.target_id, r.outcome, r.reason) for r in results] == [
        ("AA:BB", "failed", "unexpected_error"), ("later", "skipped", "unsupported_brand")]


def test_tuya_explicit_rejection_falls_back_then_reports_failure(real_lights, monkeypatch):
    monkeypatch.setattr(lights, "_secrets_cache", {"plug1": {"ip": "192.0.2.9", "local_key": SENTINEL}})
    fake_tinytuya(monkeypatch, local=[{"Error": SENTINEL, "Err": "914"}])
    result = only(TUYA)
    assert (result.outcome, result.reason, result.transport) == ("failed", "tuya_rejected", "local")
    assert [a.reason for a in result.attempts] == ["tuya_rejected", "missing_credentials"]
    assert SENTINEL not in repr(result)


def test_tuya_partial_local_then_rejected_brightness(real_lights, monkeypatch):
    monkeypatch.setattr(lights, "_secrets_cache", {"plug1": {"ip": "192.0.2.9", "local_key": "k"}})
    fake_tinytuya(monkeypatch, local=[{"dps": {"1": True}}, {"Error": "x"}])
    result = only(TUYA)
    assert (result.outcome, result.progress) == ("failed", ("turn",))


def test_tuya_unrecognized_return_is_unconfirmed_without_fallback(real_lights, monkeypatch):
    monkeypatch.setattr(lights, "_secrets_cache", {"plug1": {"ip": "192.0.2.9", "local_key": "k"}})
    monkeypatch.setenv("TUYA_ACCESS_ID", "id")
    monkeypatch.setenv("TUYA_ACCESS_KEY", "key")
    fake_tinytuya(monkeypatch, local=[None, None])
    result = only(TUYA)
    assert (result.outcome, result.reason) == ("unconfirmed", "result_unconfirmed")
    assert len(result.attempts) == 1  # a clean return is not retried on another transport


def test_tuya_cloud_success_and_rejection(real_lights, monkeypatch):
    monkeypatch.setenv("TUYA_ACCESS_ID", "id")
    monkeypatch.setenv("TUYA_ACCESS_KEY", SENTINEL)
    fake_tinytuya(monkeypatch, cloud=[{"success": True, "result": True}] * 2)
    result = only(TUYA)
    assert (result.outcome, result.reason, result.credential_source) == (
        "request_accepted_by_transport", "cloud_accepted", "environment")
    fake_tinytuya(monkeypatch, cloud=[{"success": False, "msg": SENTINEL, "code": 1106}])
    result = only(TUYA)
    assert (result.outcome, result.reason, result.progress) == ("failed", "tuya_rejected", ())
    assert SENTINEL not in repr(result)


def test_tuya_exceptions_carry_no_text(real_lights, monkeypatch):
    monkeypatch.setattr(lights, "_secrets_cache", {"plug1": {"ip": "192.0.2.9", "local_key": "k"}})
    fake_tinytuya(monkeypatch, local_error=RuntimeError(SENTINEL))
    result = only(TUYA)
    assert (result.outcome, result.reason) == ("failed", "send_error")
    assert SENTINEL not in repr(result)


# --- dispatcher records ----------------------------------------------------------


def test_sequence_links_receipt_queue_results_and_summary(client, real_lights, lan, monkeypatch):
    monkeypatch.setattr(lights, "_secrets_cache", {"AA:BB": {"ip": "192.0.2.5"}})
    assert post(client, payload("media.play", uuid="uuid-living")).status_code == 200
    assert post(client, payload("media.pause", uuid="uuid-living")).status_code == 200
    assert post(client, payload("media.play", uuid="uuid-living")).status_code == 200
    assert post(client, payload("media.stop", uuid="uuid-living")).status_code == 200
    drain()
    log = rows(client)
    receipts = [r for r in log if r["action"] == "webhook.receipt"]
    assert len(receipts) == 4
    for receipt, request in zip(receipts, ["dim", "restore", "dim", "restore"]):
        chain = [r for r in log if r["correlation_id"] == receipt["correlation_id"]]
        assert [r["action"] for r in chain] == [
            "webhook.receipt", "light.action_queued", "light.result", "light.action_summary"]
        assert [r["id"] for r in chain] == sorted(r["id"] for r in chain)
        queued, result, summary = chain[1:]
        assert {r["detail"]["action_id"] for r in chain[1:]} == {queued["detail"]["action_id"]}
        uuid.UUID(queued["detail"]["action_id"])
        assert queued["detail"]["request"] == request
        assert queued["detail"]["event_id"] == int(receipt["target_id"])
        assert queued["detail"]["targets"] == [
            {"brand": "govee", "id": "AA:BB", "brightness": 20 if request == "dim" else 100}]
        assert queued["detail"]["on_behalf_of"] == {"kind": "plex_server", "id": "plex-server", "verified": 0}
        assert (result["target_id"], result["outcome"]) == ("AA:BB", "command_sent")
        assert (summary["outcome"], summary["reason_code"]) == ("completed_unverified", "all_sent")
        assert all(r["checksum"] == audit.checksum(r) for r in chain)


def test_two_clients_keep_first_dim_last_restore_in_order(client, real_lights, lan):
    post(client, payload("media.play", title="Bedroom Phone", uuid="uuid-bed-phone"))
    post(client, payload("media.play", title="Bedroom Apple TV"))
    post(client, payload("media.stop", title="Bedroom Apple TV"))
    post(client, payload("media.stop", title="Bedroom Phone", uuid="uuid-bed-phone"))
    drain()
    queued = [r for r in rows(client) if r["action"] == "light.action_queued"]
    assert [r["detail"]["request"] for r in queued] == ["dim", "restore"]
    assert [r["id"] for r in queued] == sorted(r["id"] for r in queued)


def test_reload_after_enqueue_does_not_change_queued_targets(client, real_lights, lan, rooms_file, monkeypatch):
    gate, started = threading.Event(), threading.Event()
    real = lights.GoveeController._send_lan_command

    def slow(self, ip, message):
        started.set()
        gate.wait(timeout=30)
        return real(self, ip, message)

    monkeypatch.setattr(lights, "_secrets_cache", {"AA:BB": {"ip": "192.0.2.5"}, "NEW": {"ip": "192.0.2.6"}})
    monkeypatch.setattr(lights.GoveeController, "_send_lan_command", slow)
    post(client, payload("media.play", uuid="uuid-living"))
    assert started.wait(timeout=10)
    rooms_file.write_text(rooms_file.read_text().replace('id: "AA:BB"', 'id: "NEW"'), encoding="utf-8")
    assert client.post("/rooms/reload").status_code == 200
    post(client, payload("media.stop", uuid="uuid-living"))  # queued after reload: sees the new light
    gate.set()
    drain()
    results = [r for r in rows(client) if r["action"] == "light.result"]
    assert [r["target_id"] for r in results] == ["AA:BB", "NEW"]
    queued = [r for r in rows(client) if r["action"] == "light.action_queued"]
    assert [t["id"] for r in queued for t in r["detail"]["targets"]] == ["AA:BB", "NEW"]


def test_mixed_results_summary_and_continuation(client, real_lights, lan, rooms_file, monkeypatch):
    rooms_file.write_text("""
rooms:
  living_room:
    plex_clients: [{title: Living Room TV, uuid: uuid-living}]
    lights:
      - {brand: govee, id: "AA:BB", model: H6159}
      - {brand: hue, id: "other"}
      - {brand: govee, id: "CC:DD", model: H6159}
""", encoding="utf-8")
    assert client.post("/rooms/reload").status_code == 200
    monkeypatch.setattr(lights, "_secrets_cache", {"AA:BB": {"ip": "192.0.2.5"}, "CC:DD": {"ip": "192.0.2.6"}})
    post(client, payload("media.play", uuid="uuid-living"))
    drain()
    log = rows(client)
    assert [(r["target_id"], r["outcome"]) for r in log if r["action"] == "light.result"] == [
        ("AA:BB", "command_sent"), ("other", "skipped"), ("CC:DD", "command_sent")]
    summary = [r for r in log if r["action"] == "light.action_summary"][0]
    assert (summary["outcome"], summary["reason_code"]) == ("partial", "mixed_results")
    assert summary["detail"]["counts"] == {"command_sent": 2, "skipped": 1}


def test_dispatcher_error_summary_and_metric_semantics(client, real_lights, monkeypatch):
    def boom(action, room_lights):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(lights, "apply_action", boom)
    labels = {"room": "living_room", "action": "dim"}
    before = dispatcher.DISPATCH_ACTIONS_TOTAL.labels(**labels)._value.get()
    assert post(client, payload("media.play", uuid="uuid-living")).json() == {"status": "received", "event": "media.play"}
    drain()
    summary = [r for r in rows(client) if r["action"] == "light.action_summary"][0]
    assert (summary["outcome"], summary["reason_code"]) == ("failed", "dispatcher_error")
    assert dispatcher.DISPATCH_ACTIONS_TOTAL.labels(**labels)._value.get() == before

    monkeypatch.setattr(lights, "apply_action", REAL_APPLY_ACTION)
    post(client, payload("media.stop", uuid="uuid-living"))
    post(client, payload("media.play", uuid="uuid-living"))
    drain()
    # Every light skipped or failed, yet the counter still counts the completed dispatcher call.
    assert dispatcher.DISPATCH_ACTIONS_TOTAL.labels(**labels)._value.get() == before + 1


def test_backend_that_returns_nothing_is_unconfirmed(client):
    post(client, payload("media.play", uuid="uuid-living"))  # conftest recorder returns None
    drain()
    summary = [r for r in rows(client) if r["action"] == "light.action_summary"][0]
    assert (summary["outcome"], summary["reason_code"]) == ("unconfirmed", "no_results")


def test_queued_only_row_stays_incomplete_and_is_not_replayed(tmp_path):
    path = tmp_path / "audit.db"
    import sqlite3
    sqlite3.connect(path).close()
    correlation = audit.correlation_id()
    action_id = audit.correlation_id()
    audit.append(path, action="light.action_queued", outcome="queued", reason_code="accepted",
                 correlation=correlation, target_id="living_room",
                 detail={"action_id": action_id, "request": "dim", "on_behalf_of": "plex_server",
                         "targets": [{"brand": "govee", "id": "AA:BB", "brightness": 20}]})
    again = records(path)  # reopening reads what was persisted; nothing synthesizes a result
    assert [r["action"] for r in again] == ["light.action_queued"]
    assert not any(r["action"] in {"light.result", "light.action_summary"} for r in again)


def test_audit_rejects_free_text_and_unknown_detail(tmp_path):
    path = tmp_path / "audit.db"
    import sqlite3
    sqlite3.connect(path).close()
    base = {"action_id": audit.correlation_id(), "request": "dim", "on_behalf_of": "plex_server",
            "brand": "govee", "transport": "lan", "credential_source": "none", "progress": [], "attempts": []}
    call = dict(action="light.result", outcome="command_sent", reason_code="lan_command_sent",
                correlation=audit.correlation_id(), target_id="AA:BB")
    for bad in ({**base, "transport": SENTINEL}, {**base, "progress": [SENTINEL]},
                {**base, "attempts": [{"transport": "lan", "credential_source": "none", "outcome": SENTINEL,
                                       "reason_code": "send_error", "progress": []}]}):
        with pytest.raises(ValueError):
            audit.append(path, detail=bad, **call)
    with pytest.raises(ValueError):
        audit.append(path, **call)  # detail required
    with pytest.raises(ValueError):
        audit.append(path, detail=base, **{**call, "reason_code": SENTINEL})
    audit.append(path, detail={**base, "extra": SENTINEL}, **call)  # unknown keys are dropped, not stored
    assert SENTINEL not in json.dumps(records(path))


def test_secrets_never_reach_audit_export_or_logs(client, real_lights, lan, monkeypatch, caplog):
    monkeypatch.setenv("GOVEE_API_KEY", SENTINEL)
    monkeypatch.setattr(lights, "_secrets_cache", {"AA:BB": {"ip": "192.0.2.5", "local_key": SENTINEL}})
    lan.fail_on = 0

    def reject(*args, **kwargs):
        raise requests.HTTPError(SENTINEL, response=types.SimpleNamespace(text=SENTINEL))

    monkeypatch.setattr(requests, "put", reject)
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        post(client, payload("media.play", uuid="uuid-living"))
        drain()
    result = [r for r in rows(client) if r["action"] == "light.result"][0]
    assert (result["outcome"], result["reason_code"]) == ("failed", "request_failed")
    assert SENTINEL not in json.dumps(rows(client)) and SENTINEL not in caplog.text
    path = client.conn.execute("PRAGMA database_list").fetchone()[2]
    assert audit_cli.main(["--db", str(path), "export"]) == 0
