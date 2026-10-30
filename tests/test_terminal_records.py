"""The exact terminal and off-light audit records for every early-exit path, with fake lights only."""
import json
import threading
import time

import pytest
import yaml
from prometheus_client import REGISTRY

from app import db, dispatcher, lights
from conftest import drain, payload
from test_brightness import OFF, FakeController, on

ACTOR = {"kind": "plex_server", "id": "plex-server", "verified": 0}
ROOMS = {room: {"plex_clients": [{"title": room}], "lights": [{"brand": "govee", "id": room}]}
         for room in ("a", "b", "c")}


@pytest.fixture
def world(registry, rooms_file, monkeypatch):
    fake = FakeController()
    fake.state = {room: on(80) for room in ROOMS}
    monkeypatch.setitem(lights._controllers, "govee", fake)
    records = []
    context = dispatcher.AuditContext(lambda **fields: records.append(fields), "receipt", 7, ACTOR)

    def configure(automations):
        rooms_file.write_text(yaml.safe_dump({"rooms": ROOMS, "automations": automations}), encoding="utf-8")
        registry.reload()

    def event(room, name="media.play"):
        dispatcher.handle_event(payload(name, room), context)
        drain()

    configure([])
    fake.records, fake.context, fake.configure, fake.event = records, context, configure, event
    return fake


def rule(source, event, target, action):
    return {"trigger": {"room": source, "event": event}, "target_room": target, "action": action}


def summaries(records, target):
    return [r for r in records if r["action"] == "light.action_summary" and r["target_id"] == target]


def summary(record, target, request, outcome, reason, counts=None):
    """The record as written today, field and key order included; only the action id varies."""
    return {
        "action": "light.action_summary", "outcome": outcome, "reason_code": reason,
        "correlation": "receipt", "target_id": target,
        "detail": {"counts": counts or {}, "action_id": record["detail"]["action_id"], "request": request,
                   "on_behalf_of": ACTOR, "event_id": 7},
    }


def assert_identical(record, expected):
    assert json.dumps(record) == json.dumps(expected)


def decisions(decision):
    return REGISTRY.get_sample_value("plex_dispatcher_light_decisions_total",
                                     {"room": "a", "decision": decision}) or 0


def test_automation_blocked_by_target_playback(world):
    world.configure([rule("a", "dim", "b", 40)])
    world.event("b")
    world.event("a")
    record = summaries(world.records, "b")[-1]
    assert_identical(record, summary(record, "b", "dim", "skipped", "own_playback_active"))


def test_explicit_restore_yields_to_newer_source(world):
    world.configure([rule("a", "dim", "b", 40), rule("a", "restore", "b", "restore"), rule("c", "dim", "b", 60)])
    world.event("a")
    world.event("c")
    world.event("a", "media.stop")
    record = summaries(world.records, "b")[-1]
    assert_identical(record, summary(record, "b", "restore", "skipped", "owned_by_newer_source"))


def test_queued_automation_yields_to_playback_started_before_it_ran(world, monkeypatch):
    monkeypatch.setitem(dispatcher._active_clients, "b", {"b": time.monotonic()})
    dispatcher._apply("b", "dim", [{"brand": "govee", "id": "b"}], world.context, "fixed-id",
                      {"dim": 40, "restore": 80}, automation_source="a")
    assert world.commands == []
    assert world.records == [summary({"detail": {"action_id": "fixed-id"}}, "b", "dim", "skipped",
                                     "own_playback_active")]


def test_superseded_action_records_the_drop_reason(world):
    release, started = threading.Event(), threading.Event()

    def block():
        started.set()
        release.wait(timeout=10)

    dispatcher._action_executor.submit(block)
    assert started.wait(timeout=5)
    try:
        first = dispatcher._dispatch("a", "dim", world.context)
        dispatcher._dispatch("a", "restore", world.context)
        assert first.cancelled()
    finally:
        release.set()
    drain()
    dropped = summaries(world.records, "a")[0]
    assert_identical(dropped, summary(dropped, "a", "dim", "skipped", "superseded"))


def test_apply_error_records_dispatcher_error(world, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(lights, "apply_action", boom)
    world.event("a")
    record = summaries(world.records, "a")[-1]
    assert_identical(record, summary(record, "a", "dim", "failed", "dispatcher_error"))


def off_light_records(records, action_id):
    return [r for r in records if r["detail"]["action_id"] == action_id and r["action"] != "light.action_queued"]


def test_off_light_on_fresh_read_and_on_redim_records_the_same_skip(world):
    world.state["a"] = OFF
    before = decisions("dim_skipped_off")
    for _ in range(2):  # the second dim finds the first dim's was-off record
        dispatcher._dispatch("a", "dim", world.context).result(timeout=30)
        assert db.get_restore_record("a", "a") == {"was_off": True, "restore_percent": None, "dim_percent": 20}
    assert decisions("dim_skipped_off") - before == 2
    assert world.commands == []
    first, second = (off_light_records(world.records, s["detail"]["action_id"]) for s in summaries(world.records, "a"))
    for records in (first, second):
        action_id = records[0]["detail"]["action_id"]
        common = {"action_id": action_id, "request": "dim", "on_behalf_of": ACTOR, "event_id": 7}
        assert json.dumps(records) == json.dumps([
            {"action": "light.decision", "outcome": "dim_skipped_off", "reason_code": "was_off",
             "correlation": "receipt", "target_id": "a",
             "detail": {"brand": "govee", "dim_percent": 20, "restore_percent": None, "observed_percent": None,
                        **common}},
            {"action": "light.result", "outcome": "skipped", "reason_code": "light_off",
             "correlation": "receipt", "target_id": "a",
             "detail": {"brand": "govee", "transport": "none", "credential_source": "none", "progress": [],
                        "attempts": [{"transport": "none", "credential_source": "none", "outcome": "skipped",
                                      "reason_code": "light_off", "progress": []}], **common}},
            summary(records[-1], "a", "dim", "skipped", "all_skipped", {"skipped": 1}),
        ])


def test_off_light_writes_its_record_before_the_decision_and_only_on_a_fresh_read(world, monkeypatch):
    world.state["a"] = OFF
    calls = []
    put, decide = db.put_restore_record, dispatcher._decide
    monkeypatch.setattr(db, "put_restore_record", lambda *args: (calls.append(("put", args[1:])), put(*args))[1])
    monkeypatch.setattr(dispatcher, "_decide", lambda *args, **kwargs: (calls.append(("decide", args[5])),
                                                                         decide(*args, **kwargs))[1])
    for _ in range(2):
        dispatcher._dispatch("a", "dim", world.context).result(timeout=30)
    assert calls == [("put", ("a", True, None, 20)), ("decide", "dim_skipped_off"), ("decide", "dim_skipped_off")]
