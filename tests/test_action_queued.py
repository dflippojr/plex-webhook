"""The exact light.action_queued record for playback and automation actions, with fake lights only."""
import json

import pytest
import yaml

from app import dispatcher, lights
from conftest import drain, payload
from test_brightness import FakeController, on

ACTOR = {"kind": "plex_server", "id": "plex-server", "verified": 0}
ROOMS = {
    "a": {"dim_brightness_percent": 25, "plex_clients": [{"title": "a"}],
          "lights": [{"brand": "govee", "id": "a2"}, {"brand": "tuya", "id": "a1"}]},
    "b": {"plex_clients": [{"title": "b"}],
          "lights": [{"brand": "tuya", "id": "b2"}, {"brand": "govee", "id": "b1"}]},
}
AUTOMATIONS = [{"trigger": {"room": "a", "event": "dim"}, "target_room": "b", "action": 40}]


@pytest.fixture
def world(registry, rooms_file, monkeypatch):
    rooms_file.write_text(yaml.safe_dump({"rooms": ROOMS, "automations": AUTOMATIONS}), encoding="utf-8")
    registry.reload()
    fake = FakeController()
    fake.state = {light: on(80) for light in ("a1", "a2", "b1", "b2")}
    monkeypatch.setitem(lights._controllers, "govee", fake)
    monkeypatch.setitem(lights._controllers, "tuya", fake)
    records = []
    context = dispatcher.AuditContext(lambda **fields: records.append(fields), "receipt", 7, ACTOR)
    return context, records


def queued(records, target):
    return [record for record in records if record["action"] == "light.action_queued"
            and record["target_id"] == target]


def expected(record, target, request, targets):
    """The record as written today, field and key order included; only the action id varies."""
    return {
        "action": "light.action_queued", "outcome": "queued", "reason_code": "accepted",
        "correlation": "receipt", "target_id": target,
        "detail": {"targets": targets, "action_id": record["detail"]["action_id"], "request": request,
                   "on_behalf_of": ACTOR, "event_id": 7},
    }


def assert_identical(record, target, request, targets):
    assert json.dumps(record) == json.dumps(expected(record, target, request, targets))


def test_playback_dim_and_restore_records(world):
    context, records = world
    dispatcher.handle_event(payload("media.play", "a"), context)
    drain()
    dispatcher.handle_event(payload("media.stop", "a"), context)
    drain()
    dim, restore = queued(records, "a")
    assert_identical(dim, "a", "dim", [{"brand": "govee", "id": "a2", "brightness": 25},
                                       {"brand": "tuya", "id": "a1", "brightness": 25}])
    assert_identical(restore, "a", "restore", [{"brand": "govee", "id": "a2", "brightness": None},
                                               {"brand": "tuya", "id": "a1", "brightness": None}])


def test_automation_dim_at_level_and_restore_records(world):
    context, records = world
    dispatcher.handle_event(payload("media.play", "a"), context)
    drain()
    dispatcher.handle_event(payload("media.stop", "a"), context)
    drain()
    dim, restore = queued(records, "b")
    assert_identical(dim, "b", "dim", [{"brand": "tuya", "id": "b2", "brightness": 40},
                                       {"brand": "govee", "id": "b1", "brightness": 40}])
    assert_identical(restore, "b", "restore", [{"brand": "tuya", "id": "b2", "brightness": None},
                                               {"brand": "govee", "id": "b1", "brightness": None}])


def test_queued_record_precedes_the_action_it_announces(world):
    context, records = world
    dispatcher.handle_event(payload("media.play", "a"), context)
    drain()
    names = [(record["action"], record["target_id"]) for record in records]
    assert names.index(("light.action_queued", "a")) < names.index(("light.decision", "a2"))
    assert names.index(("light.action_queued", "b")) < names.index(("light.decision", "b2"))


def test_snapshot_failure_records_no_queued_action_and_reports_dispatcher_error(world, monkeypatch):
    context, records = world

    def boom(room_key):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(dispatcher.registry, "lights_for", boom)
    dispatcher._dispatch("a", "dim", context).result(timeout=30)
    assert [(record["action"], record["outcome"], record["reason_code"]) for record in records] == [
        ("light.action_summary", "failed", "dispatcher_error")]
    assert records[0]["detail"]["request"] == "dim"
