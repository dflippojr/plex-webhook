"""Govee and Tuya controllers with sockets, requests and tinytuya replaced by fakes."""
import json
import logging
import socket
import sys
import types

import pytest
import requests

from app import lights

GOVEE = {"brand": "govee", "id": "AA:BB", "name": "Lamp", "model": "H6159"}
TUYA = {"brand": "tuya", "id": "plug1", "name": "Plug"}


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    """Fake per-device secrets; tests overwrite the dict contents."""
    data = {}
    monkeypatch.setattr(lights, "_secrets_cache", data)
    return data


class FakeSocket:
    """Stands in for socket.socket; records sendto and replays scan replies."""

    instances = []
    replies = []
    fail_send = False
    source_ip = "10.0.0.9"

    def __init__(self, *args, **kwargs):
        self.sent = []
        self.closed = False
        FakeSocket.instances.append(self)

    def setsockopt(self, *args):
        pass

    def bind(self, *args):
        pass

    def settimeout(self, *args):
        pass

    def sendto(self, data, addr):
        if FakeSocket.fail_send:
            raise OSError("network unreachable")
        self.sent.append((json.loads(data.decode()), addr))

    def recvfrom(self, size):
        if not FakeSocket.replies:
            raise socket.timeout()
        return FakeSocket.replies.pop(0), (FakeSocket.source_ip, 4002)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_socket(monkeypatch):
    FakeSocket.instances, FakeSocket.replies, FakeSocket.fail_send = [], [], False
    FakeSocket.source_ip = "10.0.0.9"
    monkeypatch.setattr(lights.socket, "socket", FakeSocket)
    return FakeSocket


def scan_reply(device, ip):
    return json.dumps({"msg": {"cmd": "scan", "data": {"device": device, "ip": ip}}}).encode()


def commands(fake):
    return [(msg["msg"]["cmd"], msg["msg"]["data"]["value"], addr) for s in fake.instances for msg, addr in s.sent
            if msg["msg"]["cmd"] != "scan"]


# --- Govee ---------------------------------------------------------------------


def test_govee_lan_with_static_ip(fake_socket, secrets):
    secrets["AA:BB"] = {"ip": "192.168.1.50"}
    lights.GoveeController().apply("dim", GOVEE)
    assert commands(fake_socket) == [
        ("turn", 1, ("192.168.1.50", 4003)),
        ("brightness", 20, ("192.168.1.50", 4003)),
    ]
    assert all(s.closed for s in fake_socket.instances)


def test_govee_restore_brightness(fake_socket, secrets):
    secrets["AA:BB"] = {"ip": "192.168.1.50"}
    lights.GoveeController().apply("restore", GOVEE)
    assert commands(fake_socket)[1][:2] == ("brightness", 100)


def test_govee_lan_discovery(fake_socket):
    fake_socket.replies = [b"garbage", scan_reply("OTHER", "10.0.0.1"), scan_reply("AA:BB", "10.0.0.9")]
    lights.GoveeController().apply("dim", GOVEE)
    assert commands(fake_socket)[0] == ("turn", 1, ("10.0.0.9", 4003))


MALFORMED_SCAN_REPLIES = [
    b"null", b"[]", b'"text"', b"42", b"true", b"{}",
    b'{"msg": null}', b'{"msg": []}', b'{"msg": "text"}', b'{"msg": 42}',
    b'{"msg": {}}', b'{"msg": {"data": null}}', b'{"msg": {"data": []}}',
    b'{"msg": {"data": "text"}}', b'{"msg": {"data": 42}}',
    b'{"msg": {"data": {}}}', b'{"msg": {"data": {"device": "AA:BB"}}}',
    pytest.param(b"[" * 1500 + b"0" + b"]" * 1500, id="deeply-nested"),
    b"garbage", b"\xff",
]


@pytest.mark.parametrize("reply", MALFORMED_SCAN_REPLIES)
def test_govee_discovery_ignores_malformed_reply(fake_socket, reply):
    fake_socket.replies = [reply]
    assert lights.GoveeController()._discover_ip(GOVEE["id"]) is None
    assert all(s.closed for s in fake_socket.instances)


@pytest.mark.parametrize("ip,source_ip", [
    ("10.0.0.8", "10.0.0.9"), ("8.8.8.8", "8.8.8.8"),
    ("127.0.0.1", "127.0.0.1"), ("169.254.1.2", "169.254.1.2"),
    ("0.0.0.0", "0.0.0.0"), ("192.0.2.1", "192.0.2.1"),
    ("239.255.255.250", "239.255.255.250"), ("255.255.255.255", "255.255.255.255"),
    ("fc00::1", "fc00::1"), ("lamp.local", "lamp.local"),
    ("10.0.0.9/24", "10.0.0.9/24"), ("010.0.0.9", "010.0.0.9"),
    (" 10.0.0.9", " 10.0.0.9"), ("", "10.0.0.9"),
    (None, "10.0.0.9"), (42, "10.0.0.9"), (True, "10.0.0.9"),
    ([], "10.0.0.9"), ({}, "10.0.0.9"),
])
def test_govee_discovery_ignores_invalid_address(fake_socket, ip, source_ip):
    fake_socket.source_ip = source_ip
    fake_socket.replies = [scan_reply(GOVEE["id"], ip)]
    assert lights.GoveeController()._discover_ip(GOVEE["id"]) is None
    assert commands(fake_socket) == []
    assert all(s.closed for s in fake_socket.instances)


@pytest.mark.parametrize("ip", ["10.0.0.9", "172.16.0.9", "192.168.1.9"])
def test_govee_discovery_accepts_private_source_address(fake_socket, ip):
    fake_socket.source_ip = ip
    fake_socket.replies = [scan_reply(GOVEE["id"], ip)]
    assert lights.GoveeController()._discover_ip(GOVEE["id"]) == ip
    assert all(s.closed for s in fake_socket.instances)


def test_govee_discovery_ignores_decoder_recursion_error(fake_socket, monkeypatch):
    fake_socket.replies = [b"deeply nested JSON"]
    # Decoder depth limits vary by interpreter and test runner.
    original_loads = json.loads

    def decode(data):
        if data == "deeply nested JSON":
            raise RecursionError("maximum recursion depth exceeded")
        return original_loads(data)

    monkeypatch.setattr(lights.json, "loads", decode)
    assert lights.GoveeController()._discover_ip(GOVEE["id"]) is None
    assert all(s.closed for s in fake_socket.instances)


def test_govee_discovery_continues_after_invalid_replies(fake_socket):
    fake_socket.replies = [b"null", b'{"msg": {"data": []}}'] + [
        scan_reply(GOVEE["id"], "10.0.0.8"), scan_reply(GOVEE["id"], "10.0.0.9"),
    ]
    assert lights.GoveeController()._discover_ip(GOVEE["id"]) == "10.0.0.9"


@pytest.mark.parametrize("reply", MALFORMED_SCAN_REPLIES)
def test_govee_malformed_discovery_falls_back_to_cloud(fake_socket, monkeypatch, reply):
    fake_socket.replies = [reply]
    monkeypatch.setenv("GOVEE_API_KEY", "dummy-key")
    puts = []

    def fake_put(url, headers, json, timeout):
        puts.append((url, headers["Govee-API-Key"], json["cmd"]))
        return types.SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "put", fake_put)
    result = lights.GoveeController().apply("dim", GOVEE)
    assert puts == [
        (lights.GOVEE_CLOUD_API_URL, "dummy-key", {"name": "turn", "value": "on"}),
        (lights.GOVEE_CLOUD_API_URL, "dummy-key", {"name": "brightness", "value": 20}),
    ]
    assert result.transport == "cloud"
    assert result.outcome == "request_accepted_by_transport"
    assert result.attempts[0].reason == "no_address"
    assert commands(fake_socket) == []
    assert all(s.closed for s in fake_socket.instances)


def test_govee_falls_back_to_cloud_when_lan_fails(fake_socket, secrets, monkeypatch):
    secrets["AA:BB"] = {"ip": "192.168.1.50"}
    fake_socket.fail_send = True
    monkeypatch.setenv("GOVEE_API_KEY", "key")
    puts = []

    def fake_put(url, headers, json, timeout):
        puts.append((url, headers["Govee-API-Key"], json["cmd"]))
        return types.SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "put", fake_put)
    lights.GoveeController().apply("dim", GOVEE)
    assert puts == [
        (lights.GOVEE_CLOUD_API_URL, "key", {"name": "turn", "value": "on"}),
        (lights.GOVEE_CLOUD_API_URL, "key", {"name": "brightness", "value": 20}),
    ]


def test_govee_cloud_error_does_not_raise(fake_socket, monkeypatch, caplog):
    monkeypatch.setenv("GOVEE_API_KEY", "key")

    def boom(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "put", boom)
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        lights.GoveeController().apply("dim", GOVEE)
    assert "could not control light" in caplog.text


def test_govee_cloud_needs_model(fake_socket, monkeypatch, caplog):
    monkeypatch.setenv("GOVEE_API_KEY", "key")
    monkeypatch.setattr(requests, "put", lambda *a, **k: pytest.fail("cloud must not be called"))
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        lights.GoveeController().apply("dim", {**GOVEE, "model": None})
    assert "no 'model'" in caplog.text


def test_govee_no_credentials_no_raise(fake_socket, caplog):
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        lights.GoveeController().apply("dim", GOVEE)
    assert "could not control light" in caplog.text


def test_govee_discovery_oserror_is_swallowed(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no sockets")

    monkeypatch.setattr(lights.socket, "socket", broken)
    lights.GoveeController().apply("dim", GOVEE)
    assert lights.GoveeController()._discover_ip(None) is None


def test_govee_unexpected_error_is_caught(monkeypatch, caplog):
    controller = lights.GoveeController()
    monkeypatch.setattr(controller, "_try_lan", lambda *a: 1 / 0)
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        controller.apply("dim", GOVEE)
    assert "unexpected_controller_error" in caplog.text


# --- Tuya ----------------------------------------------------------------------


class FakeTinytuya(types.ModuleType):
    def __init__(self, local_fails=False, cloud_fails=False):
        super().__init__("tinytuya")
        self.log = []
        outer = self

        class OutletDevice:
            def __init__(self, dev_id, ip, key):
                if local_fails:
                    raise OSError("unreachable")
                outer.log.append(("local", dev_id, ip, key))

            def set_version(self, v):
                pass

            def set_socketTimeout(self, t):
                pass

            def turn_on(self):
                outer.log.append(("turn_on",))

            def set_value(self, dp, value):
                outer.log.append(("set_value", dp, value))

        class Cloud:
            def __init__(self, **kwargs):
                outer.log.append(("cloud", kwargs["apiKey"]))

            def sendcommand(self, dev_id, commands):
                if cloud_fails:
                    raise OSError("cloud down")
                outer.log.append(("cmd", dev_id, commands[0]["code"], commands[0]["value"]))

        self.OutletDevice, self.Cloud = OutletDevice, Cloud


def install_tinytuya(monkeypatch, **kwargs):
    fake = FakeTinytuya(**kwargs)
    monkeypatch.setitem(sys.modules, "tinytuya", fake)
    return fake


def test_tuya_local(monkeypatch, secrets):
    secrets["plug1"] = {"local_key": "k", "ip": "10.0.0.5"}
    fake = install_tinytuya(monkeypatch)
    lights.TuyaController().apply("dim", TUYA)
    assert fake.log == [("local", "plug1", "10.0.0.5", "k"), ("turn_on",), ("set_value", 3, 200)]


def test_tuya_falls_back_to_cloud(monkeypatch, secrets):
    secrets["plug1"] = {"local_key": "k", "ip": "10.0.0.5"}
    monkeypatch.setenv("TUYA_ACCESS_ID", "id")
    monkeypatch.setenv("TUYA_ACCESS_KEY", "secret")
    fake = install_tinytuya(monkeypatch, local_fails=True)
    lights.TuyaController().apply("restore", TUYA)
    assert fake.log == [
        ("cloud", "id"),
        ("cmd", "plug1", "switch_1", True),
        ("cmd", "plug1", "bright_value_v2", 1000),
    ]


def test_tuya_cloud_failure_does_not_raise(monkeypatch, caplog):
    monkeypatch.setenv("TUYA_ACCESS_ID", "id")
    monkeypatch.setenv("TUYA_ACCESS_KEY", "secret")
    install_tinytuya(monkeypatch, cloud_fails=True)
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        lights.TuyaController().apply("dim", TUYA)
    assert "could not control light" in caplog.text


def test_tuya_no_credentials_no_raise(monkeypatch, caplog):
    fake = install_tinytuya(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        lights.TuyaController().apply("dim", TUYA)
    assert fake.log == []
    assert "could not control light" in caplog.text


def test_tuya_missing_library_no_raise(monkeypatch, secrets):
    secrets["plug1"] = {"local_key": "k", "ip": "10.0.0.5"}
    monkeypatch.setenv("TUYA_ACCESS_ID", "id")
    monkeypatch.setenv("TUYA_ACCESS_KEY", "secret")
    monkeypatch.setitem(sys.modules, "tinytuya", None)  # makes `import tinytuya` raise ImportError
    lights.TuyaController().apply("dim", TUYA)


def test_tuya_unexpected_error_is_caught(monkeypatch, caplog):
    controller = lights.TuyaController()
    monkeypatch.setattr(controller, "_try_local", lambda *a: 1 / 0)
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        controller.apply("dim", TUYA)
    assert "unexpected_controller_error" in caplog.text


# --- dispatch table and secrets ---------------------------------------------------


def test_apply_action_routes_by_brand(monkeypatch, caplog):
    seen = []
    monkeypatch.setitem(lights._controllers, "govee", types.SimpleNamespace(apply=lambda a, l, b=None: seen.append(("govee", a))))
    monkeypatch.setitem(lights._controllers, "tuya", types.SimpleNamespace(apply=lambda a, l, b=None: seen.append(("tuya", a))))
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        lights.apply_action("dim", [GOVEE, TUYA, {"brand": "hue", "id": "x"}])
    assert seen == [("govee", "dim"), ("tuya", "dim")]
    assert "reason=controller_unavailable brand=unknown" in caplog.text


def test_load_secrets_reads_file_and_caches(monkeypatch, tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("devices:\n  plug1:\n    ip: 1.2.3.4\n", encoding="utf-8")
    monkeypatch.setattr(lights, "SECRETS_PATH", path)
    monkeypatch.setattr(lights, "_secrets_cache", None)
    assert lights._device_secret("plug1") == {"ip": "1.2.3.4"}
    path.unlink()
    assert lights._device_secret("plug1") == {"ip": "1.2.3.4"}  # cached
    assert lights._device_secret("other") == {}


def test_load_secrets_missing_or_broken_file(monkeypatch, tmp_path):
    monkeypatch.setattr(lights, "SECRETS_PATH", tmp_path / "absent.yaml")
    monkeypatch.setattr(lights, "_secrets_cache", None)
    assert lights._load_secrets() == {}

    broken = tmp_path / "broken.yaml"
    broken.write_text("devices: [unclosed", encoding="utf-8")
    monkeypatch.setattr(lights, "SECRETS_PATH", broken)
    monkeypatch.setattr(lights, "_secrets_cache", None)
    assert lights._load_secrets() == {}
