"""Characterization of the shared apply/read_state loops: exact log lines and results per brand."""
import logging

import pytest

from app import lights

GOVEE = {"brand": "govee", "id": "AA:BB", "name": "Lamp", "model": "H6159"}
TUYA = {"brand": "tuya", "id": "plug1", "name": "Plug"}

# (controller, brand, light, first transport, its apply step, its read step)
BRANDS = [
    pytest.param(lights.GoveeController, "govee", GOVEE, "lan", "_try_lan", "_read_lan", id="govee"),
    pytest.param(lights.TuyaController, "tuya", TUYA, "local", "_try_local", "_read_local", id="tuya"),
]


def _messages(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "plex-webhook"]


def _boom(*args):
    raise RuntimeError("secret detail")


def _failed(transport):
    return lambda *args: lights.Attempt(transport, "failed", "request_failed", "environment")


def _accepted(transport):
    return lambda *args: lights.Attempt(transport, "request_accepted_by_transport", "cloud_accepted", "environment")


@pytest.mark.parametrize("cls,brand,light,first,try_first,read_first", BRANDS)
def test_success_on_first_transport_logs_one_info_line(cls, brand, light, first, try_first, read_first,
                                                       monkeypatch, caplog):
    controller = cls()
    monkeypatch.setattr(controller, try_first, _accepted(first))
    monkeypatch.setattr(controller, "_try_cloud", _boom)
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        result = controller.apply("restore", light, 55, turn_on=False)
    assert _messages(caplog) == [
        (logging.INFO, f"{brand}[{first}]: {light['name']} -> brightness=55% ({light['id']})"),
    ]
    assert (result.brand, result.transport, result.outcome) == (brand, first, "request_accepted_by_transport")
    assert [a.transport for a in result.attempts] == [first]


@pytest.mark.parametrize("cls,brand,light,first,try_first,read_first", BRANDS)
def test_success_after_fallback_logs_the_cloud_line(cls, brand, light, first, try_first, read_first,
                                                    monkeypatch, caplog):
    controller = cls()
    monkeypatch.setattr(controller, try_first, _failed(first))
    monkeypatch.setattr(controller, "_try_cloud", _accepted("cloud"))
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        result = controller.apply("dim", light)
    assert _messages(caplog) == [
        (logging.INFO, f"{brand}[cloud]: {light['name']} -> on, brightness=20% ({light['id']})"),
    ]
    assert (result.transport, result.outcome) == ("cloud", "request_accepted_by_transport")
    assert [a.transport for a in result.attempts] == [first, "cloud"]


@pytest.mark.parametrize("cls,brand,light,first,try_first,read_first", BRANDS)
def test_exhaustion_logs_one_warning(cls, brand, light, first, try_first, read_first, monkeypatch, caplog):
    controller = cls()
    monkeypatch.setattr(controller, try_first, _failed(first))
    monkeypatch.setattr(controller, "_try_cloud", _failed("cloud"))
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        result = controller.apply("dim", light)
    assert _messages(caplog) == [
        (logging.WARNING,
         f"reason=control_unavailable brand={brand} operation=apply id={light['id']} "
         "fallback=exhausted (could not control light)"),
    ]
    assert (result.brand, result.transport, result.outcome, result.reason) == (
        brand, "cloud", "failed", "request_failed")


@pytest.mark.parametrize("action,logged", [("dim", "dim"), ("<script>", "unknown")])
@pytest.mark.parametrize("cls,brand,light,first,try_first,read_first", BRANDS)
def test_unexpected_error_logs_one_warning(cls, brand, light, first, try_first, read_first, action, logged,
                                           monkeypatch, caplog):
    controller = cls()
    monkeypatch.setattr(controller, try_first, _boom)
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        result = controller.apply(action, light)
    assert _messages(caplog) == [
        (logging.WARNING,
         f"reason=unexpected_controller_error brand={brand} operation=apply action={logged} id={light['id']}"),
    ]
    assert (result.brand, result.transport, result.outcome, result.reason) == (
        brand, "none", "failed", "unexpected_error")


@pytest.mark.parametrize("cls,brand,light,first,try_first,read_first", BRANDS)
def test_non_result_from_apply_is_unconfirmed(cls, brand, light, first, try_first, read_first, monkeypatch, caplog):
    controller = cls()
    monkeypatch.setattr(controller, "_apply", lambda *args: None)
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        result = controller.apply("dim", light)
    assert _messages(caplog) == []
    assert (result.brand, result.outcome, result.reason) == (brand, "unconfirmed", "result_unconfirmed")


@pytest.mark.parametrize("cls,brand,light,first,try_first,read_first", BRANDS)
def test_read_state_returns_the_first_ok_transport(cls, brand, light, first, try_first, read_first,
                                                   monkeypatch, caplog):
    controller = cls()
    monkeypatch.setattr(controller, read_first, lambda *args: lights.StateReading(True, True, 40))
    monkeypatch.setattr(controller, "_read_cloud", _boom)
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        reading = controller.read_state(light, 1.0)
    assert reading == lights.StateReading(True, True, 40, first)
    assert _messages(caplog) == []


@pytest.mark.parametrize("cls,brand,light,first,try_first,read_first", BRANDS)
def test_read_state_falls_back_after_a_raise_or_a_failed_read(cls, brand, light, first, try_first, read_first,
                                                              monkeypatch, caplog):
    controller = cls()
    monkeypatch.setattr(controller, read_first, _boom)
    monkeypatch.setattr(controller, "_read_cloud", lambda *args: lights.StateReading(True, False))
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        reading = controller.read_state(light, 1.0)
    assert reading == lights.StateReading(True, False, None, "cloud")
    assert _messages(caplog) == [
        (logging.INFO, f"reason=state_read_failed brand={brand} operation=read_state id={light['id']}"),
    ]

    caplog.clear()
    monkeypatch.setattr(controller, read_first, lambda *args: lights.StateReading.failed())
    assert controller.read_state(light, 1.0) == lights.StateReading(True, False, None, "cloud")
    assert _messages(caplog) == []


@pytest.mark.parametrize("cls,brand,light,first,try_first,read_first", BRANDS)
def test_read_state_fails_when_both_raise(cls, brand, light, first, try_first, read_first, monkeypatch, caplog):
    controller = cls()
    monkeypatch.setattr(controller, read_first, _boom)
    monkeypatch.setattr(controller, "_read_cloud", _boom)
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        reading = controller.read_state(light, 1.0)
    assert reading == lights.StateReading.failed()
    line = (logging.INFO, f"reason=state_read_failed brand={brand} operation=read_state id={light['id']}")
    assert _messages(caplog) == [line, line]
