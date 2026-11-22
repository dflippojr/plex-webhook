"""Graceful shutdown (#185 stage 4): intake stops, the queue drains for a bounded time, the store closes."""
import threading

import pytest
from fastapi.testclient import TestClient

from app import lights, service as service_module
from app.action_queue import ActionQueue
from app.dispatcher import CheckDropped
from app.main import create_app
from conftest import ADMIN_TOKEN, accepted_result, payload, post, records


def test_close_lets_queued_work_finish_within_the_bound():
    queue, done = ActionQueue(limit=8, name="test-queue"), []
    futures = [queue.submit(done.append, n) for n in range(3)]
    assert queue.close(5) is True
    assert done == [0, 1, 2] and all(f.done() and not f.cancelled() for f in futures)


def test_close_drops_what_is_still_queued_at_the_deadline_and_never_interrupts_the_running_action():
    queue, release, started, reasons = ActionQueue(limit=8, name="test-queue"), threading.Event(), threading.Event(), []

    def block():
        started.set()
        release.wait(timeout=10)
        return "finished"

    running = queue.submit(block)
    assert started.wait(timeout=5)
    queued = queue.submit(lambda: pytest.fail("a dropped entry must never run"), on_drop=reasons.append)
    assert queue.close(0.05) is False
    assert reasons == ["shutdown"] and queued.cancelled()
    assert not running.done()  # still running: not interrupted, not cancelled
    release.set()
    assert running.result(timeout=5) == "finished"


def test_submit_after_close_is_dropped_as_shutdown_and_starts_no_worker():
    queue, reasons = ActionQueue(limit=8, name="test-queue"), []
    assert queue.close(1) is True
    future = queue.submit(lambda: pytest.fail("submitted after close"), on_drop=reasons.append)
    assert reasons == ["shutdown"] and future.cancelled()
    assert queue.pending() == 0


def test_a_check_after_shutdown_is_dropped_as_shutdown(playback):
    playback.queue.close(1)
    with pytest.raises(CheckDropped) as exc:
        playback.check_lights("living_room").result(timeout=5)
    assert exc.value.reason == "shutdown"


def test_a_light_check_refused_by_shutdown_is_503_and_audited(client):
    client.service.queue.close(1)
    response = client.get("/rooms/living_room/lights")
    assert response.status_code == 503 and response.json() == {"detail": "Light check stopped by shutdown"}
    path = client.service.settings.db_path
    check = [r for r in records(path) if r["action"] == "lights.check"]
    assert [(r["outcome"], r["reason_code"]) for r in check] == [("failed", "shutdown")]


def _summaries(path):
    return [(r["target_id"], r["detail"]["request"], r["outcome"], r["reason_code"])
            for r in records(path) if r["action"] == "light.action_summary"]


def _queued(path):
    return [(r["target_id"], r["detail"]["request"]) for r in records(path) if r["action"] == "light.action_queued"]


def test_shutdown_lets_a_queued_action_complete(settings, monkeypatch, light_calls):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    with TestClient(create_app(settings)) as client:
        assert post(client, payload("media.play")).status_code == 200
    # No drain before exit: the lifespan's shutdown let the queued dim finish.
    assert light_calls == [("dim", ["AA:BB"])]
    assert _summaries(settings.db_path) == [("living_room", "dim", "completed_unverified", "all_sent")]


def test_shutdown_audits_actions_that_never_ran_and_leaves_the_running_one_incomplete(settings, monkeypatch,
                                                                                         light_calls):
    """A queued action either completes or is audited: never-started ones get skipped/shutdown."""
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(service_module, "SHUTDOWN_DRAIN_SECONDS", 0.2)
    release, started = threading.Event(), threading.Event()

    def held(action, room_lights, brightness=None, turn_on=True):
        started.set()
        release.wait(timeout=10)
        return [accepted_result(light) for light in room_lights]

    monkeypatch.setattr(lights, "apply_action", held)
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            assert post(client, payload("media.play")).status_code == 200
            assert started.wait(timeout=5)  # the living-room dim holds the worker
            assert post(client, payload("media.play", " Bedroom Apple TV ")).status_code == 200
        # Shutdown waited 0.2 s, then dropped the bedroom dim it never started.
        assert app.state.service.conn is None
        assert _queued(settings.db_path) == [("living_room", "dim"), ("bedroom", "dim")]
        assert _summaries(settings.db_path) == [("bedroom", "dim", "skipped", "shutdown")]
        dropped = app.state.service.metrics.registry.get_sample_value(
            "plex_dispatcher_actions_dropped_total", {"room": "bedroom", "reason": "shutdown"})
        assert dropped == 2  # the bedroom dim and its queued automation batch
    finally:
        release.set()
    # The running dim was never interrupted; it records its own summary if it finishes before exit.
    finished = ("living_room", "dim", "completed_unverified", "all_sent")
    for _ in range(500):
        if finished in _summaries(settings.db_path):
            break
        threading.Event().wait(0.01)
    assert finished in _summaries(settings.db_path)


MANY_ROOMS = "rooms:\n" + "".join(
    f"  room_{n}:\n    plex_clients:\n      - {{title: Synthetic TV {n}}}\n    lights:\n      - {{brand: govee, id: l{n}}}\n"
    for n in range(12))


def test_shutdown_audits_dropped_actions_within_its_deadline_under_lock_contention(rooms_file, make_service,
                                                                                    monkeypatch, caplog):
    """Terminal records for dropped actions share the one shutdown deadline; what is left is logged by count."""
    import sqlite3
    import time

    from app import audit

    rooms_file.write_text(MANY_ROOMS, encoding="utf-8")
    monkeypatch.setattr(service_module, "SHUTDOWN_DRAIN_SECONDS", 0.5)
    release, started = threading.Event(), threading.Event()

    def held(action, room_lights, brightness=None, turn_on=True):
        started.set()
        release.wait(timeout=30)
        return [accepted_result(light) for light in room_lights]

    monkeypatch.setattr(lights, "apply_action", held)
    monkeypatch.setattr(lights, "read_state", lambda light, timeout=None: lights.StateReading.failed())
    monkeypatch.setattr(lights, "prepare", lambda room_lights: None)
    service = make_service()
    for n in range(12):
        context = service.light_audit_context(audit.correlation_id(), None)
        service.playback.handle_event(payload("media.play", f"Synthetic TV {n}"), context)
        if n == 0:
            assert started.wait(timeout=5)  # room_0's dim holds the worker; the rest stay queued
    locker = sqlite3.connect(service.settings.db_path, timeout=0)
    locker.execute("BEGIN IMMEDIATE")  # another writer holds the lock for the whole shutdown
    try:
        start = time.monotonic()
        service.stop()
        elapsed = time.monotonic() - start
    finally:
        locker.rollback()
        locker.close()
        release.set()
    # One deadline for draining and records; at most one record's 0.25 s lock wait past it.
    assert elapsed < 0.5 + 0.25 + 0.3, elapsed
    unaudited = [r.getMessage() for r in caplog.records if "reason=shutdown_drops_unaudited" in r.getMessage()]
    assert len(unaudited) == 1
