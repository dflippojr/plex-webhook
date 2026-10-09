"""Cross-room lifecycle behavior with real restore storage and fake lights only."""
import threading

import pytest
import yaml

from app import db, dispatcher, lights
from app.rooms import RoomConfigError
from conftest import drain, payload
from test_brightness import FakeController, OFF, on


def rule(source="a", event="dim", target="b", action=40):
    return {"trigger": {"room": source, "event": event}, "target_room": target, "action": action}


@pytest.fixture
def world(registry, rooms_file, monkeypatch):
    fake = FakeController()
    fake.state = {room: on(80) for room in ("a", "b", "c")}
    monkeypatch.setitem(lights._controllers, "govee", fake)
    rooms = {room: {"plex_clients": [{"title": room}], "lights": [{"brand": "govee", "id": room}]}
             for room in fake.state}

    def configure(entries):
        rooms_file.write_text(yaml.safe_dump({"rooms": rooms, "automations": entries}), encoding="utf-8")
        registry.reload()

    configure([rule()])
    fake.configure = configure
    return fake


def event(room, name="media.play", wait=True):
    dispatcher.handle_event(payload(name, room))
    if wait:
        drain()


@pytest.mark.parametrize("restore_event", ["media.pause", "media.stop"])
def test_dim_then_source_restore_restores_target(world, restore_event):
    event("a")
    assert world.commands == [("dim", "a", 20), ("dim", "b", 40)]
    assert db.automation_targets("a") == {"b"}
    event("a", restore_event)
    assert world.commands[-2:] == [("restore", "a", 80), ("restore", "b", 80)]
    assert db.automation_targets("a") == set()
    assert db.rooms_with_restore_records() == set()


def test_restore_trigger_can_set_a_level_and_next_restore_cleans_it_up(world):
    world.configure([rule(event="restore", action=55)])
    event("a")
    event("a", "media.stop")
    assert world.commands[-1] == ("dim", "b", 55)
    event("a")
    event("a", "media.pause")
    assert world.commands[-2:] == [("restore", "b", 80), ("dim", "b", 55)]


def test_explicit_restore_action_and_latest_trigger(world):
    world.configure([rule(), rule("c", "dim", "b", "restore")])
    event("a")
    event("c")
    assert world.commands[-1] == ("restore", "b", 80)
    assert db.automation_targets("a") == set()
    before = list(world.commands)
    event("a", "media.stop")
    assert world.commands == before + [("restore", "a", 80)]


@pytest.mark.parametrize("target_action", [40, "restore"])
def test_target_playback_blocks_automation_dim_and_restore(world, target_action):
    world.configure([rule(action=target_action), rule(event="restore", action=target_action)])
    event("b")
    before = [command for command in world.commands if command[1] == "b"]
    event("a")
    event("a", "media.stop")
    assert [command for command in world.commands if command[1] == "b"] == before
    assert world.state["b"].brightness == 20
    event("b", "media.stop")
    assert world.commands[-1] == ("restore", "b", 80)


def test_target_playback_takes_over_existing_automation_record(world):
    event("a")
    event("b")
    assert db.get_restore_record("b", "b") == {"was_off": False, "restore_percent": 80, "dim_percent": 20}
    assert db.automation_targets("a") == set()
    event("a", "media.stop")
    assert world.state["b"].brightness == 20
    event("b", "media.stop")
    assert world.state["b"].brightness == 80


def test_latest_automation_owns_restore_and_preserves_original_level(world):
    world.configure([rule(), rule("c", action=60)])
    event("a")
    event("c")
    assert world.state["b"].brightness == 60
    assert db.automation_targets("a") == set()
    assert db.automation_targets("c") == {"b"}
    event("a", "media.stop")
    assert world.state["b"].brightness == 60
    event("c", "media.stop")
    assert world.commands[-1] == ("restore", "b", 80)


@pytest.mark.parametrize("manual", [on(70), OFF])
def test_manual_change_blocks_automated_restore(world, manual):
    event("a")
    world.state["b"] = manual
    event("a", "media.stop")
    assert world.state["b"] == manual
    assert [command for command in world.commands if command[1] == "b"] == [("dim", "b", 40)]
    assert db.get_restore_record("b", "b") is None


def test_off_light_stays_off(world):
    world.state["b"] = OFF
    event("a")
    event("a", "media.stop")
    assert all(command[1] != "b" for command in world.commands)
    assert world.state["b"] == OFF


def test_restart_restores_targets_even_when_source_has_no_lights(world, registry):
    registry.rooms["a"]["lights"] = []
    event("a")
    dispatcher._active_clients.clear()
    dispatcher._pending_rooms.clear()
    dispatcher._load_pending_rooms()
    assert "a" in dispatcher._pending_rooms
    event("a", "media.stop")
    assert world.commands[-1] == ("restore", "b", 80)


def test_automation_changes_do_not_trigger_other_automations(world):
    world.configure([rule(), rule("b", target="c")])
    event("a")
    assert all(command[1] != "c" for command in world.commands)


def test_config_reload_keeps_pending_restore_owner(world):
    event("a")
    world.configure([])
    event("a", "media.stop")
    assert world.commands[-1] == ("restore", "b", 80)


def test_queued_automation_yields_to_new_playback(world):
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(timeout=10)

    dispatcher._action_executor.submit(block)
    assert started.wait(timeout=5)
    try:
        event("a", wait=False)
        event("b", wait=False)
    finally:
        release.set()
        drain()
    assert [command for command in world.commands if command[1] == "b"] == [("dim", "b", 20)]


def test_queued_automation_snapshots_config(world):
    release = threading.Event()
    dispatcher._action_executor.submit(lambda: release.wait(timeout=10))
    try:
        event("a", wait=False)
        world.configure([rule(action=90)])
    finally:
        release.set()
        drain()
    assert world.commands[-1] == ("dim", "b", 40)


def test_automation_is_audited_with_receipt_correlation(world):
    records = []
    context = dispatcher.AuditContext(lambda **fields: records.append(fields), "source-receipt")
    dispatcher.handle_event(payload("media.play", "a"), context)
    drain()
    queued = [record for record in records if record["action"] == "light.action_queued"
              and record["target_id"] == "b"]
    assert queued[0]["correlation"] == "source-receipt"
    assert queued[0]["detail"]["targets"] == [{"brand": "govee", "id": "b", "brightness": 40}]
    action_id = queued[0]["detail"]["action_id"]
    assert any(record["action"] == "light.action_summary" and record["detail"]["action_id"] == action_id
               for record in records)


def test_queued_restore_yields_to_new_target_playback(world):
    event("a")
    release = threading.Event()
    dispatcher._action_executor.submit(lambda: release.wait(timeout=10))
    try:
        event("a", "media.stop", wait=False)
        event("b", wait=False)
    finally:
        release.set()
        drain()
    assert [command for command in world.commands if command[1] == "b"] == [
        ("dim", "b", 40), ("dim", "b", 20)]
    event("b", "media.stop")
    assert world.commands[-1] == ("restore", "b", 80)


INVALID_RULES = [
    (None, "automations[0]", "expected_mapping"),
    ({}, "automations[0].trigger", "expected_mapping"),
    ({**rule(), "trigger": []}, "automations[0].trigger", "expected_mapping"),
    (rule(source="private-value"), "automations[0].trigger.room", "unknown_room"),
    (rule(target="private-value"), "automations[0].target_room", "unknown_room"),
    (rule(target="a"), "automations[0].target_room", "self_target"),
    (rule(event="pause"), "automations[0].trigger.event", "expected_lifecycle_event"),
    *[(rule(action=value), "automations[0].action", "expected_automation_action")
      for value in (0, 101, -1, 40.5, True, False, None, "40", {}, [])],
]


@pytest.mark.parametrize("entry,path,code", INVALID_RULES)
def test_invalid_rules_preserve_previous_config(world, registry, rooms_file, entry, path, code):
    before = registry._state
    document = yaml.safe_load(rooms_file.read_text(encoding="utf-8"))
    document["automations"] = [entry]
    rooms_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(RoomConfigError) as error:
        registry.reload()
    assert error.value.detail == [{"path": path, "code": code}]
    assert registry._state is before
    assert "private-value" not in str(error.value.detail)


@pytest.mark.parametrize("entries", [None, {}, "private-value"])
def test_automations_must_be_a_list(world, entries):
    with pytest.raises(RoomConfigError) as error:
        world.configure(entries)
    assert error.value.detail == [{"path": "automations", "code": "expected_list"}]


@pytest.mark.parametrize("entries", [
    [rule(), rule("b", "restore", "a", "restore")],
    [rule(), rule("b", target="c"), rule("c", target="a")],
])
def test_cycles_rejected_across_all_trigger_types(world, entries):
    with pytest.raises(RoomConfigError) as error:
        world.configure(entries)
    assert error.value.detail == [{"path": "automations", "code": "automation_cycle"}]


def test_valid_boundaries_and_repeat_rules(world):
    world.configure([rule(action=1), rule(action=100), rule(event="restore", action="restore")])
    event("a")
    assert world.commands[-1] == ("dim", "b", 100)
    event("a", "media.stop")
    assert world.state["b"].brightness == 80
