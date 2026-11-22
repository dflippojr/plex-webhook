"""HassController against a fake Home Assistant hub: only requests.get/post are replaced."""
import json
from types import SimpleNamespace

import pytest
import requests

from app import audit, lights

URL = "http://hub.invalid:8123"
TOKEN = "synthetic-hass-token-for-unit-tests"
LIGHT = {"brand": "hass", "id": "light.synthetic_lamp", "name": "Lamp"}


class FakeHub:
    """Records every request; answers from ``states`` (entity id -> (status, body)) and ``post_status``."""

    def __init__(self):
        self.calls, self.states, self.post_status, self.raise_with = [], {}, 200, None

    def get(self, url, *, headers, timeout, allow_redirects):
        self.calls.append(("GET", url, headers, None, timeout, allow_redirects))
        if self.raise_with:
            raise self.raise_with
        status, body = self.states.get(url.rsplit("/", 1)[-1], (404, {"message": "Entity not found."}))
        text = body if isinstance(body, str) else json.dumps(body)
        return SimpleNamespace(status_code=status, json=lambda: json.loads(text))

    def post(self, url, *, headers, json, timeout, allow_redirects):
        self.calls.append(("POST", url, headers, json, timeout, allow_redirects))
        if self.raise_with:
            raise self.raise_with
        return SimpleNamespace(status_code=self.post_status)


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    monkeypatch.setenv("HASS_URL", URL + "/")
    monkeypatch.setenv("HASS_TOKEN", TOKEN)
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


def state(value, brightness=None, status=200):
    attributes = {"friendly_name": "Lamp"}
    if brightness is not None:
        attributes["brightness"] = brightness
    return status, {"entity_id": LIGHT["id"], "state": value, "attributes": attributes}


def read(hub, reply, light=LIGHT):
    hub.states[light["id"]] = reply
    return lights.read_state(light, 1.5)


def test_hass_is_registered():
    assert isinstance(lights.get_controller("hass"), lights.HassController)


# --- reads -------------------------------------------------------------------


@pytest.mark.parametrize("brightness, percent", [(255, 100), (128, 50), (3, 1), (1, 1), (0, 1)])
def test_on_light_reads_its_brightness_as_a_percent(hub, brightness, percent):
    assert read(hub, state("on", brightness)) == lights.StateReading(True, True, percent, "hub")
    [(method, url, headers, body, timeout, redirects)] = hub.calls
    assert (method, url, body, timeout, redirects) == ("GET", f"{URL}/api/states/light.synthetic_lamp", None, 1.5,
                                                       False)
    assert headers == {"Authorization": f"Bearer {TOKEN}"}


def test_off_light_reads_as_off(hub):
    assert read(hub, state("off")) == lights.StateReading(True, False, None, "hub")


@pytest.mark.parametrize("reply", [
    pytest.param(state("unavailable"), id="unavailable"),
    pytest.param(state("unknown", 100), id="unknown"),
    pytest.param(state("on", 128, status=500), id="status-500"),
    pytest.param(state("on", 128, status=401), id="status-401"),
    pytest.param(state("on", 128, status=301), id="redirect"),
    pytest.param((404, {"message": "Entity not found."}), id="status-404"),
    pytest.param((200, "not json {"), id="invalid-json"),
    pytest.param((200, ["on"]), id="not-an-object"),
    pytest.param(state("on"), id="on-without-brightness"),
    pytest.param(state("on", 256), id="brightness-above-255"),
    pytest.param(state("on", -1), id="brightness-negative"),
    pytest.param(state("on", 12.5), id="brightness-float"),
    pytest.param(state("on", True), id="brightness-bool"),
    pytest.param(state("on", "128"), id="brightness-string"),
    pytest.param((200, {"state": "on", "attributes": None}), id="attributes-null"),
])
def test_unreadable_replies_fail_without_raising(hub, reply):
    assert read(hub, reply) == lights.StateReading.failed()


@pytest.mark.parametrize("error", [requests.Timeout("timed out"), requests.ConnectionError("refused"),
                                   ValueError("boom")])
def test_timeouts_and_connection_errors_fail_the_read(hub, error):
    hub.raise_with = error
    assert read(hub, state("on", 255)) == lights.StateReading.failed()
    assert len(hub.calls) == 1


# --- commands ----------------------------------------------------------------


@pytest.mark.parametrize("action, brightness", [("dim", 20), ("restore", 70)])
def test_dim_and_restore_send_one_turn_on_call(hub, action, brightness):
    result = lights.apply_action(action, [LIGHT], brightness)[0]
    assert (result.brand, result.target_id, result.transport, result.credential_source, result.outcome,
            result.reason, result.progress) == (
        "hass", LIGHT["id"], "hub", "environment", "request_accepted_by_transport", "hub_accepted",
        ("turn", "brightness"))
    [(method, url, headers, body, timeout, redirects)] = hub.calls
    assert (method, url, redirects) == ("POST", f"{URL}/api/services/light/turn_on", False)
    assert headers == {"Authorization": f"Bearer {TOKEN}"}
    assert body == {"entity_id": LIGHT["id"], "brightness_pct": brightness}
    assert timeout == lights.HASS_COMMAND_TIMEOUT_S


def test_per_light_brightness_and_default_levels(hub):
    other = {"brand": "hass", "id": "light.synthetic_other"}
    lights.apply_action("restore", [LIGHT, other], {LIGHT["id"]: 65})
    assert [call[3] for call in hub.calls] == [
        {"entity_id": LIGHT["id"], "brightness_pct": 65},
        {"entity_id": other["id"], "brightness_pct": lights.DEFAULT_RESTORE_PERCENT},
    ]


@pytest.mark.parametrize("status", [199, 300, 400, 401, 404, 500, 503])
def test_non_2xx_reply_fails_with_a_fixed_reason(hub, status):
    hub.post_status = status
    result = lights.apply_action("dim", [LIGHT], 20)[0]
    assert (result.transport, result.outcome, result.reason) == ("hub", "failed", "hub_rejected")
    assert result.progress == ()


@pytest.mark.parametrize("status", [200, 201, 299])
def test_any_2xx_reply_is_accepted(hub, status):
    hub.post_status = status
    assert lights.apply_action("dim", [LIGHT], 20)[0].reason == "hub_accepted"


@pytest.mark.parametrize("error", [requests.Timeout("timed out"), requests.ConnectionError("refused"),
                                   RuntimeError("boom")])
def test_request_exception_fails_with_a_fixed_reason(hub, error):
    hub.raise_with = error
    result = lights.apply_action("dim", [LIGHT], 20)[0]
    assert (result.transport, result.outcome, result.reason) == ("hub", "failed", "request_failed")


def test_dim_of_an_unread_light_sends_nothing(hub):
    result = lights.apply_action("dim", [LIGHT], 20, turn_on=False)[0]
    assert (result.transport, result.outcome, result.reason) == ("hub", "skipped", "brightness_only_unsupported")
    assert hub.calls == []


@pytest.mark.parametrize("unset", ["HASS_URL", "HASS_TOKEN"])
def test_missing_hub_settings_skip_without_a_request(hub, monkeypatch, unset):
    monkeypatch.delenv(unset)
    result = lights.apply_action("dim", [LIGHT], 20)[0]
    assert (result.transport, result.credential_source, result.outcome, result.reason) == (
        "hub", "none", "skipped", "missing_credentials")
    assert lights.read_state(LIGHT) == lights.StateReading.failed()
    assert hub.calls == []


@pytest.mark.parametrize("url", ["   ", "hub.invalid:8123", "ftp://hub.invalid", "file:///etc/passwd", "http://",
                                 "http://hub.invalid/?x=1", "http://hub.invalid/#frag", "http://[broken"])
def test_unusable_hub_url_is_treated_as_missing(hub, monkeypatch, url):
    monkeypatch.setenv("HASS_URL", url)
    assert lights.apply_action("dim", [LIGHT], 20)[0].reason == "missing_credentials"
    assert lights.read_state(LIGHT) == lights.StateReading.failed()
    assert hub.calls == []


def test_hub_url_with_a_path_prefix_is_kept(hub, monkeypatch):
    monkeypatch.setenv("HASS_URL", "https://hub.invalid/ha/")
    lights.apply_action("dim", [LIGHT], 20)
    assert hub.calls[0][1] == "https://hub.invalid/ha/api/services/light/turn_on"


@pytest.mark.parametrize("entity_id", [
    "light.a/../../api/config", "light.a/b", "light..", "../light.a", "light.a?x=1", "light.a#x", "light.a\n",
    "light.A", "light.", "switch.plug", "light.a b", "light.a%2f", "light.a.b", "", None, 7, ["light.a"],
])
def test_entity_id_outside_the_pattern_sends_nothing(hub, entity_id):
    light = {"brand": "hass", "id": entity_id}
    result = lights.apply_action("dim", [light], 20)[0]
    assert (result.outcome, result.reason) == ("skipped", "invalid_entity_id")
    assert lights.read_state(light) == lights.StateReading.failed()
    assert hub.calls == []


def test_apply_never_raises_when_the_step_itself_breaks(hub, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(lights.HassController, "_try_hub", broken)
    result = lights.get_controller("hass").apply("dim", LIGHT, 20)
    assert (result.outcome, result.reason) == ("failed", "unexpected_error")


def test_every_result_uses_the_fixed_vocabulary(hub, monkeypatch):
    outcomes = audit.OPERATIONS["light.result"][3]
    results = [lights.apply_action("dim", [LIGHT], 20)[0],
               lights.apply_action("dim", [LIGHT], 20, turn_on=False)[0],
               lights.apply_action("dim", [{"brand": "hass", "id": "bad/id"}], 20)[0]]
    hub.post_status = 500
    results.append(lights.apply_action("dim", [LIGHT], 20)[0])
    hub.raise_with = OSError("boom")
    results.append(lights.apply_action("dim", [LIGHT], 20)[0])
    monkeypatch.delenv("HASS_TOKEN")
    results.append(lights.apply_action("dim", [LIGHT], 20)[0])
    for result in results:
        assert result.transport in audit.LIGHT_TRANSPORTS
        assert result.credential_source in audit.LIGHT_CREDENTIAL_SOURCES
        assert result.reason in outcomes[result.outcome]
        for attempt in result.attempts:
            assert attempt.reason in outcomes[attempt.outcome]
