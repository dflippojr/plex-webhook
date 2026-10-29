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


@pytest.mark.parametrize("reply", [
    pytest.param(b"null", id="decoded-wrong-shape"),
    pytest.param(b"garbage", id="json-decode-failure"),
])
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


# --- Govee discovery cache -------------------------------------------------------

ROOM = [{"brand": "govee", "id": f"G{n}", "name": f"Bulb {n}", "model": "H6159"} for n in range(3)]


class LanSocket:
    """A fake Govee LAN: scans are answered by `bulbs`, devStatus by `status` (ip -> reply)."""

    bulbs, status, scans, commands, clock = {}, {}, 0, [], None

    def __init__(self, *args):
        self.inbox = []

    setsockopt = bind = settimeout = close = lambda self, *a: None

    def sendto(self, data, addr):
        message = json.loads(data.decode())["msg"]
        if message["cmd"] == "scan":
            LanSocket.scans += 1
            self.inbox += [(scan_reply(device, ip), ip) for device, ip in LanSocket.bulbs.items()]
        elif message["cmd"] == "devStatus":
            if addr[0] in LanSocket.status:
                self.inbox.append((json.dumps(LanSocket.status[addr[0]]).encode(), addr[0]))
        else:
            LanSocket.commands.append((message["cmd"], addr[0]))

    def recvfrom(self, size):
        if not self.inbox:
            raise socket.timeout()
        data, ip = self.inbox.pop(0)
        return data, (ip, 4002)


@pytest.fixture
def lan(monkeypatch):
    LanSocket.bulbs, LanSocket.status, LanSocket.scans, LanSocket.commands = {}, {}, 0, []
    monkeypatch.setattr(lights.socket, "socket", LanSocket)
    clock = types.SimpleNamespace(now=1000.0)
    monkeypatch.setattr(lights, "_clock", lambda: clock.now)
    LanSocket.clock = clock
    return LanSocket


def on_at(level):
    return {"msg": {"cmd": "devStatus", "data": {"onOff": 1, "brightness": level}}}


def full_dim(room=ROOM):
    """The dispatcher's dim sequence: prepare, read every light, then command them."""
    lights.prepare(room)
    readings = [lights.read_state(light) for light in room]
    return readings, lights.apply_action("dim", room, 20)


def test_unanswered_room_scans_once_per_dim_and_falls_back(lan):
    readings, results = full_dim()
    assert lan.scans == 1
    assert readings == [lights.StateReading.failed()] * 3
    assert [(r.outcome, r.reason) for r in results] == [("skipped", "missing_credentials")] * 3
    assert [r.attempts[0].reason for r in results] == ["no_address"] * 3
    assert lan.commands == []


def test_unanswered_room_uses_cloud_when_configured(lan, monkeypatch):
    monkeypatch.setenv("GOVEE_API_KEY", "key")
    body = {"data": {"properties": [{"powerState": "on"}, {"brightness": 70}]}}
    monkeypatch.setattr(requests, "get", lambda *a, **k: types.SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: body))
    monkeypatch.setattr(requests, "put", lambda *a, **k: types.SimpleNamespace(raise_for_status=lambda: None))
    readings, results = full_dim()
    assert lan.scans == 1
    assert readings == [lights.StateReading(True, True, 70, "cloud")] * 3
    assert [(r.transport, r.outcome) for r in results] == [("cloud", "request_accepted_by_transport")] * 3
    assert [r.attempts[0].reason for r in results] == ["no_address"] * 3


def test_missing_devices_are_scanned_for_again_after_a_minute(lan):
    full_dim()
    lan.clock.now += lights.GOVEE_DISCOVERY_MISS_TTL_S - 1
    full_dim()
    assert lan.scans == 1
    lan.clock.now += 1
    full_dim()
    assert lan.scans == 2


def test_answered_room_is_cached_for_the_ttl(lan):
    lan.bulbs = {light["id"]: f"10.0.0.{n + 1}" for n, light in enumerate(ROOM)}
    lan.status = {ip: on_at(60) for ip in lan.bulbs.values()}
    readings, results = full_dim()
    assert lan.scans == 1
    assert readings == [lights.StateReading(True, True, 60, "lan")] * 3
    assert [(r.transport, r.outcome, r.credential_source) for r in results] == [("lan", "command_sent", "none")] * 3
    assert lan.commands[:2] == [("turn", "10.0.0.1"), ("brightness", "10.0.0.1")]

    lan.clock.now += lights.GOVEE_DISCOVERY_TTL_S - 1
    full_dim()
    assert lan.scans == 1
    lan.clock.now += 1
    full_dim()
    assert lan.scans == 2


def test_failed_devstatus_at_a_cached_address_scans_again_next_action(lan):
    lan.bulbs, lan.status = {"G0": "10.0.0.1"}, {"10.0.0.1": on_at(40)}
    room = ROOM[:1]
    full_dim(room)
    assert lan.scans == 1
    # The lease moved: no devStatus answer at the cached address drops the entry.
    lan.status = {}
    lights.prepare(room)
    assert lan.scans == 1
    assert lights.read_state(room[0]) == lights.StateReading.failed()
    assert lights._govee_addresses == {}
    lan.bulbs = {"G0": "10.0.0.7"}
    lan.status = {"10.0.0.7": on_at(40)}
    lights.prepare(room)
    assert lan.scans == 2
    assert lights.read_state(room[0]) == lights.StateReading(True, True, 40, "lan")


def test_static_ip_never_scans(lan, secrets):
    secrets.update({light["id"]: {"ip": f"192.168.1.{n + 1}"} for n, light in enumerate(ROOM)})
    lan.status = {"192.168.1.1": on_at(50)}
    readings, results = full_dim()
    assert lan.scans == 0
    assert readings[0] == lights.StateReading(True, True, 50, "lan")
    assert [(r.outcome, r.credential_source) for r in results] == [("command_sent", "device_config")] * 3
    assert lights._govee_addresses == {}  # a failed read at a static ip has nothing to drop


def test_one_scan_resolves_only_the_lights_that_need_it(lan, secrets, monkeypatch):
    secrets["G0"] = {"ip": "192.168.1.1"}
    lan.bulbs = {"G1": "10.0.0.2"}
    seen = []
    original = lights.GoveeController._discover_ips

    def discover(self, device_ids, timeout=lights.GOVEE_LAN_SCAN_TIMEOUT_S):
        seen.append(list(device_ids))
        return original(self, device_ids, timeout)

    monkeypatch.setattr(lights.GoveeController, "_discover_ips", discover)
    lights.prepare(ROOM + [TUYA, ROOM[1], {"brand": "govee"}, "not-a-light"])
    assert seen == [["G1", "G2"]]
    assert lights._govee_addresses["G1"][0] == "10.0.0.2"
    assert lights._govee_addresses["G2"][0] is None


def test_batch_scan_matches_each_reply_to_its_own_device(fake_socket):
    fake_socket.replies = [scan_reply("G1", "10.0.0.9"), scan_reply("OTHER", "10.0.0.9"), scan_reply("G0", "10.0.0.9")]
    assert lights.GoveeController()._discover_ips(["G0", "G1", "G2"]) == {"G0": "10.0.0.9", "G1": "10.0.0.9"}
    assert sum(msg["msg"]["cmd"] == "scan" for s in fake_socket.instances for msg, _ in s.sent) == 1


def test_batch_scan_stops_once_every_device_answered(fake_socket):
    fake_socket.replies = [scan_reply("G0", "10.0.0.9"), scan_reply("G0", "10.0.0.9")]
    assert lights.GoveeController()._discover_ips(["G0"]) == {"G0": "10.0.0.9"}
    assert len(fake_socket.replies) == 1


def test_direct_calls_share_the_cache_without_prepare(lan):
    lan.bulbs = {"G0": "10.0.0.1"}
    lan.status = {"10.0.0.1": on_at(30)}
    assert lights.read_state(ROOM[0]) == lights.StateReading(True, True, 30, "lan")
    lights.GoveeController().apply("dim", ROOM[0])
    assert lan.scans == 1
    assert lan.commands == [("turn", "10.0.0.1"), ("brightness", "10.0.0.1")]


def test_prepare_never_raises(monkeypatch, caplog):
    monkeypatch.setattr(lights.GoveeController, "prepare", lambda self, room_lights: 1 / 0)
    with caplog.at_level(logging.INFO, logger="plex-webhook"):
        lights.prepare(ROOM)
    assert "operation=prepare" in caplog.text


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
    monkeypatch.setitem(lights._controllers, "govee", types.SimpleNamespace(apply=lambda a, l, b=None, t=True: seen.append(("govee", a))))
    monkeypatch.setitem(lights._controllers, "tuya", types.SimpleNamespace(apply=lambda a, l, b=None, t=True: seen.append(("tuya", a))))
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


# --- brightness only (a light whose state is unknown must not be turned on) -------


def test_govee_lan_brightness_only_sends_no_turn(fake_socket, secrets):
    secrets["AA:BB"] = {"ip": "192.168.1.50"}
    result = lights.GoveeController().apply("dim", GOVEE, 30, turn_on=False)
    assert commands(fake_socket) == [("brightness", 30, ("192.168.1.50", 4003))]
    assert result.progress == ("brightness",)


def test_govee_cloud_brightness_only_sends_no_turn(fake_socket, monkeypatch):
    monkeypatch.setenv("GOVEE_API_KEY", "key")
    puts = []

    def fake_put(url, headers, json, timeout):
        puts.append(json["cmd"])
        return types.SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "put", fake_put)
    result = lights.GoveeController().apply("dim", GOVEE, 30, turn_on=False)
    assert puts == [{"name": "brightness", "value": 30}]
    assert (result.transport, result.progress) == ("cloud", ("brightness",))


@pytest.mark.parametrize("light,expected", [
    (TUYA, [("local", "plug1", "10.0.0.5", "k"), ("set_value", 3, 300)]),
    ({**TUYA, "switch_dp": 20, "brightness_dp": 22}, [("local", "plug1", "10.0.0.5", "k"), ("set_value", 22, 300)]),
])
def test_tuya_local_brightness_only_sends_no_switch(monkeypatch, secrets, light, expected):
    secrets["plug1"] = {"local_key": "k", "ip": "10.0.0.5"}
    fake = install_tinytuya(monkeypatch)
    result = lights.apply_action("dim", [light], 30, turn_on=False)[0]
    assert fake.log == expected
    assert result.progress == ("brightness",)


def test_tuya_cloud_brightness_only_sends_no_switch(monkeypatch):
    monkeypatch.setenv("TUYA_ACCESS_ID", "id")
    monkeypatch.setenv("TUYA_ACCESS_KEY", "secret")
    fake = install_tinytuya(monkeypatch)
    result = lights.TuyaController().apply("dim", TUYA, 30, turn_on=False)
    assert fake.log == [("cloud", "id"), ("cmd", "plug1", "bright_value_v2", 300)]
    assert (result.transport, result.progress) == ("cloud", ("brightness",))


# --- default brightness levels (dim 20, restore 100 unless a valid level is given) -----


@pytest.mark.parametrize("action,default", [("dim", 20), ("restore", 100)])
@pytest.mark.parametrize("brightness", [None, 0, 101, True, "50"])
def test_missing_or_invalid_brightness_uses_the_action_default(action, default, brightness):
    assert lights._level(action, brightness) == default


@pytest.mark.parametrize("action", ["dim", "restore"])
@pytest.mark.parametrize("brightness", [1, 50, 100])
def test_valid_brightness_is_kept(action, brightness):
    assert lights._level(action, brightness) == brightness
