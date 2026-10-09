"""Per-room coalescing and the bounded light-action queue, with fake lights and a temporary database only."""
import logging
import sqlite3
import threading
import uuid
from types import SimpleNamespace

import pytest
import yaml

from app import audit, db, dispatcher, lights
from conftest import drain, payload
from test_brightness import FakeController, on

ROOMS = ("a", "b", "c")


@pytest.fixture
def world(registry, rooms_file, monkeypatch):
    fake = FakeController()
    fake.state = {room: on(80) for room in ROOMS}
    monkeypatch.setitem(lights._controllers, "govee", fake)
    rooms = {room: {"plex_clients": [{"title": room}], "lights": [{"brand": "govee", "id": room}]}
             for room in ROOMS}
    rooms_file.write_text(yaml.safe_dump({"rooms": rooms}), encoding="utf-8")
    registry.reload()
    return fake


@pytest.fixture
def trail(tmp_path):
    """An AuditContext that appends through the real audit contract and keeps what it stored."""
    path = tmp_path / "audit.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(audit.SCHEMA)
    stored, errors = [], []

    def record(**fields):
        try:
            audit.append(path, **fields)
        except Exception as exc:  # _record would swallow it; surface it to the test instead
            errors.append((fields["action"], fields["outcome"], fields["reason_code"], exc))
            raise
        stored.append(fields)

    return SimpleNamespace(context=dispatcher.AuditContext(record, str(uuid.uuid4())), stored=stored, errors=errors)


@pytest.fixture
def blocked_worker():
    """Hold the worker busy so later submissions stay queued until released."""
    release, started = threading.Event(), threading.Event()

    def block():
        started.set()
        release.wait(timeout=10)

    dispatcher._action_executor.submit(block)
    assert started.wait(timeout=5)
    yield release
    release.set()
    drain()


def event(room, name, context=None):
    dispatcher.handle_event(payload(name, room), context)


def queued_keys():
    with dispatcher._action_executor._ready:
        return [entry[0] for entry in dispatcher._action_executor._entries]


def unbounded(monkeypatch):
    """Restore the default limit so drain()'s own submission cannot push out a queued action."""
    monkeypatch.setattr(dispatcher._action_executor, "limit", dispatcher.ACTION_QUEUE_LIMIT)


def dropped(room, reason):
    return dispatcher.DISPATCH_DROPPED_TOTAL.labels(room=room, reason=reason)._value.get()


# --- the queue on its own ---------------------------------------------------------


def test_same_key_keeps_only_the_latest_at_the_back():
    queue = dispatcher.ActionQueue(limit=8, name="test-queue")
    release = threading.Event()
    ran, drops = [], []
    queue.submit(release.wait, 10)
    first = queue.submit(ran.append, "x1", key="x", on_drop=drops.append)
    queue.submit(ran.append, "y1", key="y")
    last = queue.submit(ran.append, "x2", key="x", on_drop=drops.append)
    assert queue.pending() == 2
    release.set()
    last.result(timeout=5)
    assert ran == ["y1", "x2"]
    assert first.cancelled() and drops == ["superseded"]


def test_unkeyed_submissions_never_coalesce():
    queue = dispatcher.ActionQueue(limit=8, name="test-queue")
    ran = []
    futures = [queue.submit(ran.append, n) for n in range(3)]
    for future in futures:
        future.result(timeout=5)
    assert ran == [0, 1, 2]


def test_overflow_drops_the_oldest_queued_entry():
    queue = dispatcher.ActionQueue(limit=2, name="test-queue")
    release, started = threading.Event(), threading.Event()
    ran, drops = [], []
    queue.submit(lambda: (started.set(), release.wait(10)))
    assert started.wait(timeout=5)  # running, so it no longer counts against the limit
    oldest = queue.submit(ran.append, 1, key=1, on_drop=lambda reason: drops.append((1, reason)))
    queue.submit(ran.append, 2, key=2, on_drop=lambda reason: drops.append((2, reason)))
    newest = queue.submit(ran.append, 3, key=3, on_drop=lambda reason: drops.append((3, reason)))
    assert queue.pending() == 2
    release.set()
    newest.result(timeout=5)
    assert ran == [2, 3]
    assert oldest.cancelled() and drops == [(1, "queue_overflow")]


def test_failing_action_does_not_stop_the_worker():
    queue = dispatcher.ActionQueue(limit=4, name="test-queue")
    failing = queue.submit(lambda: 1 / 0)
    assert queue.submit(lambda: "ok").result(timeout=5) == "ok"
    assert isinstance(failing.exception(timeout=5), ZeroDivisionError)


# --- dispatcher behavior ------------------------------------------------------------


@pytest.mark.parametrize("last", ["media.play", "media.stop"])
def test_alternating_edges_leave_one_pending_action_and_the_last_state(world, trail, blocked_worker, last):
    edges = ["media.play", "media.stop"] * 10 + ([] if last == "media.stop" else ["media.play"])
    before = dropped("a", "superseded")
    for name in edges:
        event("a", name, trail.context)
        keys = queued_keys()
        assert keys.count(("room", "a")) == 1
        assert keys.count(("automations", "a")) == 1
    blocked_worker.set()
    drain()
    expected = [("dim", "a", 20)] if last == "media.play" else []
    assert world.commands == expected  # a superseded dim never touched the light
    assert world.state["a"] == (on(20) if last == "media.play" else on(80))
    # Every room and automation entry but the last pair was superseded.
    assert dropped("a", "superseded") - before == 2 * (len(edges) - 1)


def test_coalesced_restore_after_an_applied_dim_restores(world, trail):
    event("a", "media.play", trail.context)
    drain()
    release = threading.Event()
    dispatcher._action_executor.submit(release.wait, 10)
    try:
        for name in ["media.stop", "media.play", "media.stop"]:
            event("a", name, trail.context)
    finally:
        release.set()
        drain()
    assert world.commands == [("dim", "a", 20), ("restore", "a", 80)]
    assert db.rooms_with_restore_records() == set()


def test_different_rooms_do_not_coalesce(world, blocked_worker):
    event("a", "media.play")
    event("c", "media.play")
    assert queued_keys().count(("room", "a")) == queued_keys().count(("room", "c")) == 1
    blocked_worker.set()
    drain()
    assert sorted(world.commands) == [("dim", "a", 20), ("dim", "c", 20)]


def test_overflow_drops_oldest_counts_and_logs_once(world, trail, blocked_worker, monkeypatch, caplog):
    monkeypatch.setattr(dispatcher._action_executor, "limit", 2)
    before = dropped("a", "queue_overflow")
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        event("a", "media.play", trail.context)  # room + automation entry: the queue is full
        event("c", "media.play", trail.context)  # pushes out both of room a's entries
    assert queued_keys() == [("room", "c"), ("automations", "c")]
    assert dropped("a", "queue_overflow") - before == 2
    overflow_lines = [r for r in caplog.records if "dispatch_queue_overflow" in r.getMessage()]
    assert len(overflow_lines) == 2  # one line per dropped entry
    unbounded(monkeypatch)
    blocked_worker.set()
    drain()
    assert world.commands == [("dim", "c", 20)]


def test_audit_records_match_the_actions_applied(world, trail, blocked_worker, monkeypatch):
    monkeypatch.setattr(dispatcher._action_executor, "limit", 4)
    event("a", "media.play", trail.context)
    event("a", "media.stop", trail.context)   # supersedes a's dim
    event("b", "media.play", trail.context)   # fills the queue
    event("c", "media.play", trail.context)   # overflow pushes out a's restore and automations
    unbounded(monkeypatch)
    blocked_worker.set()
    drain()
    assert trail.errors == []
    stored = trail.stored
    queued = {r["detail"]["action_id"]: r for r in stored if r["action"] == "light.action_queued"}
    summaries = {}
    for record in stored:
        if record["action"] == "light.action_summary":
            assert record["detail"]["action_id"] not in summaries  # exactly one terminal record each
            summaries[record["detail"]["action_id"]] = record
    assert summaries.keys() == queued.keys()
    by_outcome = sorted((summaries[aid]["target_id"], queued[aid]["detail"]["request"],
                         summaries[aid]["outcome"], summaries[aid]["reason_code"]) for aid in queued)
    assert by_outcome == [
        ("a", "dim", "skipped", "superseded"),
        ("a", "restore", "skipped", "queue_overflow"),
        ("b", "dim", "completed_unverified", "all_sent"),
        ("c", "dim", "completed_unverified", "all_sent"),
    ]
    applied = {aid for aid, summary in summaries.items() if summary["reason_code"] == "all_sent"}
    touched = {r["detail"]["action_id"] for r in stored if r["action"] in {"light.decision", "light.result"}}
    assert touched == applied  # a dropped action has no decision or result
    assert sorted(world.commands) == [("dim", "b", 20), ("dim", "c", 20)]
    assert all(r["correlation"] == trail.context.correlation for r in stored)


def test_queue_limit_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("DISPATCH_QUEUE_LIMIT", "5")
    assert dispatcher._env_int("DISPATCH_QUEUE_LIMIT", 32) == 5
    for bad in ("0", "-1", "many"):
        monkeypatch.setenv("DISPATCH_QUEUE_LIMIT", bad)
        assert dispatcher._env_int("DISPATCH_QUEUE_LIMIT", 32) == 32


@pytest.mark.parametrize("reason", ["superseded", "queue_overflow", "own_playback_active"])
def test_skipped_summary_reasons_are_valid_audit_records(trail, reason):
    dispatcher._record(trail.context, "light.action_summary", "skipped", reason, "a", "dim", str(uuid.uuid4()),
                       counts={})
    assert trail.errors == [] and len(trail.stored) == 1
