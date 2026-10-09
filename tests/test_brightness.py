"""Per-room dim level and per-light restore, with fake controllers and a temporary database only."""
import json
import sqlite3
import sys
import types

import pytest
import requests
from prometheus_client import REGISTRY

from app import audit, db, dispatcher, lights
from app.rooms import RoomConfigError, RoomRegistry
from conftest import drain, payload

ROOM = "den"
YAML = """
rooms:
  den:
    dim_brightness_percent: 30
    restore_brightness_percent: 80
    plex_clients:
      - title: Den TV
    lights:
      - {brand: govee, id: g1, model: H6159}
      - {brand: tuya, id: t1}
"""


def on(percent):
    return lights.StateReading(True, True, percent)


OFF = lights.StateReading(True, False, None)


class FakeController:
    """Holds what each light 'physically' shows; commands update it like a real light would."""

    def __init__(self):
        self.state, self.commands, self.reads = {}, [], 0

    def read_state(self, light, timeout):
        self.reads += 1
        value = self.state.get(light["id"], OFF)
        if isinstance(value, Exception):
            raise value
        return value

    def apply(self, action, light, brightness=None):
        self.commands.append((action, light["id"], brightness))
        self.state[light["id"]] = on(brightness)
        return lights.LightResult(light["brand"], light["id"], "lan", "none", "command_sent", "lan_command_sent")


@pytest.fixture
def world(tmp_path, rooms_file, registry, monkeypatch):
    rooms_file.write_text(YAML, encoding="utf-8")
    registry.reload()
    fake = FakeController()
    monkeypatch.setitem(lights._controllers, "govee", fake)
    monkeypatch.setitem(lights._controllers, "tuya", fake)
    path = tmp_path / "audit.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(audit.SCHEMA)
    context = dispatcher.AuditContext(lambda **fields: audit.append(path, **fields), audit.correlation_id())
    world = types.SimpleNamespace(fake=fake, path=path, context=context)

    def play():
        dispatcher.handle_event(payload("media.play", "Den TV"), context)
        drain()

    def stop():
        dispatcher.handle_event(payload("media.stop", "Den TV"), context)
        drain()

    def decisions():
        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT target_id, outcome, reason_code, detail FROM audit_events "
                                "WHERE action = 'light.decision' ORDER BY id").fetchall()
        return [(t, o, r, json.loads(d)) for t, o, r, d in rows]

    world.play, world.stop, world.decisions = play, stop, decisions
    return world


def test_on_light_dims_then_restores_its_own_read_value(world):
    world.fake.state = {"g1": on(60), "t1": on(35)}
    world.play()
    assert world.fake.commands == [("dim", "g1", 30), ("dim", "t1", 30)]
    world.fake.commands.clear()
    world.stop()
    assert world.fake.commands == [("restore", "g1", 60), ("restore", "t1", 35)]
    assert [(t, o) for t, o, _, _ in world.decisions() if o == "restored"] == [("g1", "restored"), ("t1", "restored")]
    assert db.rooms_with_restore_records() == set()


def test_off_light_is_never_turned_on(world):
    world.fake.state = {"g1": OFF, "t1": on(50)}
    world.play()
    world.stop()
    assert world.fake.commands == [("dim", "t1", 30), ("restore", "t1", 50)]
    outcomes = [(t, o) for t, o, _, _ in world.decisions()]
    assert ("g1", "dim_skipped_off") in outcomes and ("g1", "skipped_was_off") in outcomes


@pytest.mark.parametrize("hand,expected", [
    (36, "skipped_manual_change"), (35, "restored"), (25, "restored"), (24, "skipped_manual_change"),
    (100, "skipped_manual_change"), (1, "skipped_manual_change"),
])
def test_manual_change_threshold_is_five_points_from_the_dim_level(world, hand, expected):
    world.fake.state = {"g1": on(70)}
    world.play()
    world.fake.state["g1"] = on(hand)  # someone used the light while dimmed (dim level 30)
    world.fake.commands.clear()
    world.stop()
    assert [o for t, o, _, _ in world.decisions() if t == "g1" and not o.startswith("dim")] == [expected]
    assert world.fake.commands == ([("restore", "g1", 70)] if expected == "restored" else [])


def test_light_turned_off_by_hand_is_skipped(world):
    world.fake.state = {"g1": on(70)}
    world.play()
    world.fake.state["g1"] = OFF
    world.fake.commands.clear()
    world.stop()
    assert world.fake.commands == []
    assert [d[1:3] for d in world.decisions() if d[0] == "g1"][-1] == ("skipped_manual_change", "turned_off")


def test_read_failure_at_dim_time_falls_back_to_room_restore_value(world):
    world.fake.state = {"g1": RuntimeError("unreachable")}
    world.play()
    assert ("dim", "g1", 30) in world.fake.commands  # still dimmed
    world.fake.state["g1"] = on(30)
    world.fake.commands.clear()
    world.stop()
    assert world.fake.commands == [("restore", "g1", 80)]


def test_read_failure_at_restore_time_still_restores(world):
    world.fake.state = {"g1": on(55)}
    world.play()
    world.fake.state["g1"] = RuntimeError("unreachable")
    world.fake.commands.clear()
    world.stop()
    assert ("restore", "g1", 55) in world.fake.commands
    last = [d for d in world.decisions() if d[0] == "g1"][-1]
    assert last[1:3] == ("restore_without_read", "read_failed")


def test_redim_before_restore_keeps_the_original_value(world):
    world.fake.state = {"g1": on(65)}
    world.play()
    # No restore ran (for example a restart lost the player state); the light now reads 30.
    dispatcher._active_clients.clear()
    world.play()
    world.fake.commands.clear()
    world.stop()
    assert world.fake.commands[0] == ("restore", "g1", 65)
    assert [o for t, o, _, _ in world.decisions() if t == "g1"].count("dim_kept_original") == 1


def test_value_equal_to_the_dim_level_is_never_stored(world):
    world.fake.state = {"g1": on(30)}  # already at the dim level (e.g. left over from an earlier dim)
    world.play()
    world.fake.state["g1"] = on(30)
    world.fake.commands.clear()
    world.stop()
    assert world.fake.commands[0] == ("restore", "g1", 80)


def test_restart_between_dim_and_restore_still_restores(world):
    world.fake.state = {"g1": on(45), "t1": on(90)}
    world.play()
    # Restart: in-memory player state is gone, persisted records are not.
    dispatcher._active_clients.clear()
    dispatcher._pending_rooms.clear()
    dispatcher._load_pending_rooms()
    assert dispatcher._pending_rooms == {ROOM}
    world.fake.commands.clear()
    world.stop()
    assert world.fake.commands == [("restore", "g1", 45), ("restore", "t1", 90)]


def test_stop_without_dim_or_records_does_nothing(world):
    world.stop()
    assert world.fake.commands == []


def test_decisions_are_audited_with_levels_and_counted(world):
    before = REGISTRY.get_sample_value("plex_dispatcher_light_decisions_total", {"room": ROOM, "decision": "restored"}) or 0
    world.fake.state = {"g1": on(60), "t1": on(35)}
    world.play()
    world.stop()
    restored = [d for d in world.decisions() if d[1] == "restored"]
    assert [d[0] for d in restored] == ["g1", "t1"]
    assert restored[0][3]["restore_percent"] == 60 and restored[0][3]["observed_percent"] == 30
    assert restored[0][3]["dim_percent"] == 30 and restored[0][3]["request"] == "restore"
    after = REGISTRY.get_sample_value("plex_dispatcher_light_decisions_total", {"room": ROOM, "decision": "restored"})
    assert after == before + 2


# --- validation -------------------------------------------------------------------


def candidate(rooms_file, text):
    rooms_file.write_text(text, encoding="utf-8")


def rejected(registry, rooms_file, text):
    before = registry._state
    candidate(rooms_file, text)
    with pytest.raises(RoomConfigError) as error:
        registry.reload()
    assert registry._state is before  # the previous mapping is kept
    return error.value.detail


def test_two_players_in_one_room_is_rejected(registry, rooms_file):
    detail = rejected(registry, rooms_file, "rooms: {den: {plex_clients: [{title: a}, {title: b}]}}")
    assert detail[0]["path"] == "rooms.den.plex_clients" and detail[0]["code"] == "expected_one_player"
    assert "one room per" in detail[0]["hint"]


def test_room_without_a_player_is_rejected(registry, rooms_file):
    assert rejected(registry, rooms_file, "rooms: {den: {lights: []}}")[0]["code"] == "expected_one_player"


def test_one_light_in_two_rooms_is_rejected_naming_both_paths(registry, rooms_file):
    detail = rejected(registry, rooms_file, """
rooms:
  a: {plex_clients: [{title: a}], lights: [{brand: govee, id: same}]}
  b: {plex_clients: [{title: b}], lights: [{brand: tuya, id: other}, {brand: govee, id: same}]}
""")
    assert [(d["path"], d["code"]) for d in detail] == [
        ("rooms.a.lights[0].id", "light_in_multiple_rooms"), ("rooms.b.lights[1].id", "light_in_multiple_rooms")]


@pytest.mark.parametrize("field", ["dim_brightness_percent", "restore_brightness_percent"])
@pytest.mark.parametrize("value", ["0", "101", "-5", "20.5", "'20'", "true", "false", "null", "[20]"])
def test_invalid_percent_is_rejected_with_field_path(registry, rooms_file, field, value):
    detail = rejected(registry, rooms_file, f"rooms: {{den: {{{field}: {value}, plex_clients: [{{title: a}}]}}}}")
    assert detail == [{"path": f"rooms.den.{field}", "code": "expected_percent"}]


@pytest.mark.parametrize("field", ["switch_dp", "brightness_dp"])
@pytest.mark.parametrize("value", ["0", "true", "'3'", "1.5"])
def test_invalid_tuya_dp_is_rejected_with_field_path(registry, rooms_file, field, value):
    detail = rejected(registry, rooms_file,
                      f"rooms: {{den: {{plex_clients: [{{title: a}}], lights: [{{brand: tuya, id: x, {field}: {value}}}]}}}}")
    assert detail == [{"path": f"rooms.den.lights[0].{field}", "code": "expected_integer"}]


def test_boundary_percents_and_defaults_load(rooms_file):
    candidate(rooms_file, """
rooms:
  a: {dim_brightness_percent: 1, restore_brightness_percent: 100, plex_clients: [{title: a}]}
  b: {plex_clients: [{title: b}]}
""")
    reg = RoomRegistry(rooms_file)
    assert reg.settings_for("a") == {"dim": 1, "restore": 100}
    assert reg.settings_for("b") == {"dim": 20, "restore": 100}


def test_existing_single_player_config_still_loads(registry):
    assert set(registry.rooms) == {"living_room", "bedroom"}


# --- Tuya scale ---------------------------------------------------------------------


@pytest.mark.parametrize("raw,percent", [(10, 1), (14, 1), (15, 2), (500, 50), (995, 100), (1000, 100), (1, 1), (0, 1), (2000, 100)])
def test_tuya_raw_to_percent_boundaries(raw, percent):
    assert lights.tuya_raw_to_percent(raw) == percent


@pytest.mark.parametrize("percent,raw", [(1, 10), (50, 500), (100, 1000)])
def test_tuya_percent_to_raw(percent, raw):
    assert lights.tuya_percent_to_raw(percent) == raw


# --- reading state through the real controllers (fakes underneath) ------------------------

GOVEE = {"brand": "govee", "id": "AA:BB", "model": "H6159"}
TUYA = {"brand": "tuya", "id": "plug1"}


@pytest.fixture
def secrets(monkeypatch):
    data = {}
    monkeypatch.setattr(lights, "_secrets_cache", data)
    return data


class StatusSocket:
    """UDP stand-in: answers a devStatus request from the device address."""

    reply, source, sent = None, "10.0.0.5", []

    def __init__(self, *args):
        pass

    setsockopt = bind = settimeout = close = lambda self, *a: None

    def sendto(self, data, addr):
        StatusSocket.sent.append((json.loads(data.decode()), addr))

    def recvfrom(self, size):
        if StatusSocket.reply is None:
            raise lights.socket.timeout()
        reply, StatusSocket.reply = StatusSocket.reply, None
        return json.dumps(reply).encode(), (StatusSocket.source, 4002)


def lan_reply(on_off, level):
    return {"msg": {"cmd": "devStatus", "data": {"onOff": on_off, "brightness": level}}}


def test_govee_lan_reads_devstatus(monkeypatch, secrets):
    secrets["AA:BB"] = {"ip": "10.0.0.5"}
    monkeypatch.setattr(lights.socket, "socket", StatusSocket)
    StatusSocket.sent, StatusSocket.reply, StatusSocket.source = [], lan_reply(1, 64), "10.0.0.5"
    assert lights.read_state(GOVEE) == lights.StateReading(True, True, 64)
    assert StatusSocket.sent == [({"msg": {"cmd": "devStatus", "data": {}}}, ("10.0.0.5", 4003))]
    StatusSocket.reply = lan_reply(0, 64)
    assert lights.read_state(GOVEE) == lights.StateReading(True, False, 64)


def test_govee_lan_ignores_other_senders_and_times_out(monkeypatch, secrets):
    secrets["AA:BB"] = {"ip": "10.0.0.5"}
    monkeypatch.setattr(lights.socket, "socket", StatusSocket)
    StatusSocket.reply, StatusSocket.source = lan_reply(1, 64), "10.0.0.99"
    assert lights.read_state(GOVEE, 0.2) == lights.StateReading.failed()


def test_govee_falls_back_to_cloud_state(monkeypatch, secrets):
    monkeypatch.setattr(lights.socket, "socket", StatusSocket)
    StatusSocket.reply = None
    monkeypatch.setenv("GOVEE_API_KEY", "key")
    seen = {}

    def get(url, headers, params, timeout):
        seen.update(url=url, params=params, timeout=timeout)
        body = {"code": 200, "data": {"properties": [{"online": True}, {"powerState": "on"}, {"brightness": 42}]}}
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(lights.GoveeController, "_discover_ip", lambda self, device_id, timeout=None: None)
    assert lights.read_state(GOVEE) == lights.StateReading(True, True, 42)
    assert seen["params"] == {"device": "AA:BB", "model": "H6159"} and seen["timeout"] == lights.STATE_READ_TIMEOUT_S


def test_govee_unreadable_everywhere_fails_without_raising(monkeypatch, secrets):
    monkeypatch.setattr(lights.GoveeController, "_discover_ip", lambda self, device_id, timeout=None: None)
    assert lights.read_state(GOVEE) == lights.StateReading.failed()


class FakeTuyaStatus(types.ModuleType):
    def __init__(self, local=None, cloud=None):
        super().__init__("tinytuya")
        outer = self
        self.timeouts = []

        class OutletDevice:
            def __init__(self, *args):
                if isinstance(local, Exception):
                    raise local

            set_version = lambda self, v: None
            set_socketTimeout = lambda self, t: outer.timeouts.append(t)
            status = lambda self: local

        class Cloud:
            def __init__(self, **kwargs):
                pass

            getstatus = lambda self, device: cloud

        self.OutletDevice, self.Cloud = OutletDevice, Cloud


def install(monkeypatch, **kwargs):
    fake = FakeTuyaStatus(**kwargs)
    monkeypatch.setitem(sys.modules, "tinytuya", fake)
    return fake


def test_tuya_local_status_converts_the_scale(monkeypatch, secrets):
    secrets["plug1"] = {"local_key": "k", "ip": "10.0.0.7"}
    fake = install(monkeypatch, local={"dps": {"1": True, "3": 500}})
    assert lights.read_state(TUYA) == lights.StateReading(True, True, 50)
    assert fake.timeouts == [lights.STATE_READ_TIMEOUT_S]


def test_tuya_local_honours_configured_data_points(monkeypatch, secrets):
    secrets["plug1"] = {"local_key": "k", "ip": "10.0.0.7"}
    install(monkeypatch, local={"dps": {"20": True, "22": 1000, "1": False, "3": 10}})
    assert lights.read_state({**TUYA, "switch_dp": 20, "brightness_dp": 22}) == lights.StateReading(True, True, 100)


def test_tuya_local_off_and_unreadable(monkeypatch, secrets):
    secrets["plug1"] = {"local_key": "k", "ip": "10.0.0.7"}
    install(monkeypatch, local={"dps": {"1": False, "3": 500}})
    assert lights.read_state(TUYA) == lights.StateReading(True, False, None)
    install(monkeypatch, local={"Error": "Network Error", "Err": "901"})
    assert lights.read_state(TUYA) == lights.StateReading.failed()
    install(monkeypatch, local={"dps": {"1": True}})  # on, but no level to restore from
    assert lights.read_state(TUYA) == lights.StateReading.failed()


def test_tuya_falls_back_to_cloud_status(monkeypatch, secrets):
    secrets["plug1"] = {"local_key": "k", "ip": "10.0.0.7"}
    monkeypatch.setenv("TUYA_ACCESS_ID", "id")
    monkeypatch.setenv("TUYA_ACCESS_KEY", "secret")
    cloud = {"success": True, "result": [{"code": "switch_1", "value": True}, {"code": "bright_value_v2", "value": 10}]}
    install(monkeypatch, local=OSError("unreachable"), cloud=cloud)
    assert lights.read_state(TUYA) == lights.StateReading(True, True, 1)
    install(monkeypatch, local=OSError("unreachable"), cloud={"success": False})
    assert lights.read_state(TUYA) == lights.StateReading.failed()


class RecordingDevice:
    log = []

    def __init__(self, *args):
        pass

    set_version = set_socketTimeout = lambda self, v: None
    turn_on = lambda self: RecordingDevice.log.append(("turn_on",)) or {"success": True}
    set_value = lambda self, dp, value: RecordingDevice.log.append(("set_value", dp, value)) or {"success": True}


def test_tuya_commands_use_configured_data_points_and_the_requested_level(monkeypatch, secrets):
    secrets["plug1"] = {"local_key": "k", "ip": "10.0.0.7"}
    fake = types.ModuleType("tinytuya")
    fake.OutletDevice = RecordingDevice
    monkeypatch.setitem(sys.modules, "tinytuya", fake)
    RecordingDevice.log = []
    lights.apply_action("restore", [TUYA], {"plug1": 37})
    assert RecordingDevice.log == [("turn_on",), ("set_value", 3, 370)]
    RecordingDevice.log = []
    lights.apply_action("dim", [{**TUYA, "switch_dp": 20, "brightness_dp": 22}], 30)
    assert RecordingDevice.log == [("set_value", 20, True), ("set_value", 22, 300)]
