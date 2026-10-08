"""Failure diagnostics use synthetic data only, including every exception boundary."""
import logging
import sys
import types

import pytest
import requests

from app import dispatcher, lights


def fail(message, exception=RuntimeError):
    def raise_error(*args, **kwargs):
        raise exception(message)
    return raise_error


def assert_safe(caplog, *sensitive):
    records = [r for r in caplog.records if r.name == "plex-webhook"]
    assert records
    for record in records:
        assert record.exc_info is None
        assert record.exc_text is None
        assert record.stack_info is None
        for value in sensitive:
            assert value not in caplog.text
            assert value not in repr(record.msg)
            assert value not in repr(record.args)


@pytest.mark.parametrize("failure", ["yaml", "read", "exists", "missing"])
def test_secrets_diagnostics(monkeypatch, tmp_path, caplog, failure):
    sentinel = "synthetic-secret-" + failure
    path = tmp_path / ("sensitive-path-" + failure)
    if failure != "missing":
        path.write_text("devices: [" + sentinel, encoding="utf-8")
    monkeypatch.setattr(lights, "SECRETS_PATH", path)
    monkeypatch.setattr(lights, "_secrets_cache", None)
    if failure in {"read", "exists"}:
        monkeypatch.setattr(type(path), "open" if failure == "read" else "exists",
                            fail(sentinel, OSError))
    with caplog.at_level(logging.INFO, logger="plex-webhook"):
        assert lights._load_secrets() == {}
        assert lights._load_secrets() == {}
    assert_safe(caplog, sentinel, str(path))
    assert ("secrets_missing" if failure == "missing" else "secrets_load_failed") in caplog.text
    assert len(caplog.records) == 1


@pytest.mark.parametrize("brand", ["govee", "tuya"])
def test_public_wrapper_and_later_light(monkeypatch, caplog, brand):
    controller = lights.get_controller(brand)
    sentinel = "synthetic-wrapper-" + brand
    calls = []

    def apply(action, light):
        calls.append(light["id"])
        if light["id"] == "first":
            raise RuntimeError(sentinel)

    monkeypatch.setattr(controller, "_apply", apply)
    with caplog.at_level(logging.INFO, logger="plex-webhook"):
        lights.apply_action("arbitrary-action-secret", [
            {"brand": brand, "id": "first", "name": "private-light-name"},
            {"brand": brand, "id": "second"},
        ])
    assert calls == ["first", "second"]
    assert_safe(caplog, sentinel, "private-light-name", "arbitrary-action-secret")
    assert "unexpected_controller_error" in caplog.text
    assert f"brand={brand}" in caplog.text
    assert "id=first" in caplog.text
    assert "action=unknown" in caplog.text


@pytest.mark.parametrize("stage", ["discovery", "lan", "govee_cloud", "local", "tuya_cloud"])
def test_transport_failures(monkeypatch, caplog, stage):
    sentinel = "synthetic-response-" + stage
    ip = "192.0.2.123"
    key = "synthetic-local-key"
    monkeypatch.setattr(lights, "_secrets_cache", {"target": {"ip": ip, "local_key": key}})
    monkeypatch.setenv("GOVEE_API_KEY", "synthetic-api-key")
    monkeypatch.setenv("TUYA_ACCESS_ID", "synthetic-access-id")
    monkeypatch.setenv("TUYA_ACCESS_KEY", "synthetic-access-key")
    monkeypatch.setattr(requests, "put", fail(sentinel))
    monkeypatch.setitem(sys.modules, "tinytuya", types.SimpleNamespace(
        OutletDevice=fail(sentinel), Cloud=fail(sentinel)))
    govee = lights.GoveeController()
    monkeypatch.setattr(govee, "_send_lan_command", fail(sentinel, OSError))
    monkeypatch.setattr(lights.socket, "socket", fail(sentinel, OSError))
    light = {"id": "target", "model": "fake-model", "name": "private-name"}
    operations = {
        "discovery": lambda: govee._discover_ip("target"),
        "lan": lambda: govee._try_lan(light, 20),
        "govee_cloud": lambda: govee._try_cloud(light, 20),
        "local": lambda: lights.TuyaController()._try_local(light, 20),
        "tuya_cloud": lambda: lights.TuyaController()._try_cloud(light, 20),
    }
    with caplog.at_level(logging.INFO, logger="plex-webhook"):
        assert not operations[stage]()
    assert_safe(caplog, sentinel, ip, key, "private-name", "synthetic-api-key",
                "synthetic-access-id", "synthetic-access-key")
    code = "discovery_failed" if stage == "discovery" else (
        "cloud_control_failed" if stage.endswith("cloud") else "local_control_failed")
    assert code in caplog.text
    if stage in {"discovery", "lan", "local"}:
        assert "fallback=cloud" in caplog.text


@pytest.mark.parametrize("brand", ["govee", "tuya", "private-unknown-brand"])
def test_unavailable_controller_omits_free_text(monkeypatch, caplog, brand):
    controller = lights.get_controller(brand)
    if brand in {"govee", "tuya"}:
        monkeypatch.setattr(controller, "_try_lan" if brand == "govee" else "_try_local", lambda *a: False)
        monkeypatch.setattr(controller, "_try_cloud", lambda *a: False)
    controller.apply("private-action", {"id": "target", "brand": brand, "name": "private-name"})
    assert_safe(caplog, "private-name", "private-action", "private-unknown-brand")


@pytest.mark.parametrize("failure_stage", ["lookup", "backend"])
def test_dispatcher_failure_and_later_action(monkeypatch, caplog, failure_stage):
    sentinel = "synthetic-dispatcher-" + failure_stage
    monkeypatch.setattr(dispatcher.registry, "lights_for", lambda room: [])
    monkeypatch.setattr(lights, "apply_action", lambda *a: None)
    target, attribute = (dispatcher.registry, "lights_for") if failure_stage == "lookup" else (lights, "apply_action")
    with monkeypatch.context() as patch:
        patch.setattr(target, attribute, fail(sentinel))
        dispatcher._dispatch("private-room-input", "private-action").result(timeout=5)
    assert_safe(caplog, sentinel, "private-room-input", "private-action")
    assert "dispatcher_action_failed" in caplog.text
    before = dispatcher.DISPATCH_ACTIONS_TOTAL.labels(room="test", action="restore")._value.get()
    dispatcher._dispatch("test", "restore").result(timeout=5)
    assert dispatcher.DISPATCH_ACTIONS_TOTAL.labels(room="test", action="restore")._value.get() == before + 1
