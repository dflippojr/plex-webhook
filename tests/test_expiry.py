"""Stale-play expiry and the optional Plex server allow-list, with a fake clock and recorded light actions."""
import pytest
from prometheus_client import REGISTRY

from app import audit, dispatcher
from app.rooms import RoomConfigError
from conftest import ROOMS_YAML, drain, payload, post, rows

HOUR = 60 * 60


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(dispatcher, "_clock", fake)
    return fake


def send(event, metadata=None, **player):
    body = payload(event, **player)
    body["Metadata"].update(metadata or {})
    dispatcher.handle_event(body)
    drain()


def expire():
    dispatcher.expire_idle_clients()
    drain()


def actions(light_calls):
    return [action for action, _ in light_calls]


def test_play_with_no_stop_restores_after_default_idle_period(registry, light_calls, clock):
    send("media.play")
    clock.advance(6 * HOUR - 1)
    expire()
    assert actions(light_calls) == ["dim"]
    clock.advance(1)
    expire()
    assert light_calls == [("dim", ["AA:BB"]), ("restore", ["AA:BB"])]
    assert dispatcher._active_clients["living_room"] == {}
    expire()
    assert actions(light_calls) == ["dim", "restore"]  # an expired room is restored once


def test_media_duration_sets_expiry_a_few_minutes_past_the_remaining_runtime(registry, light_calls, clock):
    send("media.play", {"duration": 2 * HOUR * 1000, "viewOffset": 30 * 60 * 1000})
    clock.advance(90 * 60 + dispatcher.DURATION_GRACE_SECONDS - 1)
    expire()
    assert actions(light_calls) == ["dim"]
    clock.advance(1)
    expire()
    assert actions(light_calls) == ["dim", "restore"]


@pytest.mark.parametrize("duration", [0, -5, True, "7200000", float("inf"), float("nan"), None])
def test_unusable_duration_falls_back_to_idle_period(registry, light_calls, clock, duration):
    send("media.play", {"duration": duration})
    assert dispatcher._active_clients["living_room"]["Living Room TV"] == clock.now + 6 * HOUR


def test_implausible_duration_is_capped(registry, light_calls, clock):
    send("media.play", {"duration": 10 ** 15})
    expected = clock.now + dispatcher.MAX_DURATION_SECONDS + dispatcher.DURATION_GRACE_SECONDS
    assert dispatcher._active_clients["living_room"]["Living Room TV"] == expected


def test_configured_idle_period(registry, rooms_file, light_calls, clock):
    rooms_file.write_text(ROOMS_YAML + "playback_idle_minutes: 30\n", encoding="utf-8")
    registry.reload()
    send("media.play")
    clock.advance(30 * 60)
    expire()
    assert actions(light_calls) == ["dim", "restore"]


def test_real_stop_still_restores_immediately_and_expiry_adds_nothing(registry, light_calls, clock):
    send("media.play")
    send("media.stop")
    assert actions(light_calls) == ["dim", "restore"]
    clock.advance(7 * HOUR)
    expire()
    assert actions(light_calls) == ["dim", "restore"]


def test_resume_pushes_expiry_out(registry, light_calls, clock):
    send("media.play")
    clock.advance(5 * HOUR)
    send("media.resume")  # the room is already active, so no second dim
    clock.advance(5 * HOUR)
    expire()
    assert actions(light_calls) == ["dim"]
    clock.advance(HOUR)
    expire()
    assert actions(light_calls) == ["dim", "restore"]


def test_stop_from_unknown_client_is_harmless_and_keeps_expiry(registry, light_calls, clock):
    send("media.stop", title="Kitchen iPad")
    send("media.stop", title="Living Room TV", uuid="never-started")
    assert light_calls == []
    send("media.play")
    send("media.stop", title="Living Room TV", uuid="never-started")
    assert actions(light_calls) == ["dim"]
    clock.advance(6 * HOUR)
    expire()
    assert actions(light_calls) == ["dim", "restore"]


def test_expiry_only_restores_the_stale_room(registry, light_calls, clock):
    send("media.play")
    clock.advance(3 * HOUR)
    send("media.play", title="Bedroom Apple TV")
    clock.advance(3 * HOUR)
    expire()
    assert light_calls == [("dim", ["AA:BB"]), ("dim", ["plug1"]), ("restore", ["AA:BB"])]


def test_expiry_updates_metrics(registry, light_calls, clock):
    labels = {"room": "living_room"}
    before = REGISTRY.get_sample_value("plex_dispatcher_expired_players_total", labels) or 0
    send("media.play")
    clock.advance(6 * HOUR)
    expire()
    assert REGISTRY.get_sample_value("plex_dispatcher_expired_players_total", labels) == before + 1
    assert REGISTRY.get_sample_value("plex_dispatcher_room_active_sessions", labels) == 0


def test_expiry_restore_is_audited_as_playback_expiry(client, light_calls, clock):
    import app.main as main

    post(client, payload("media.play"))
    drain()
    clock.advance(6 * HOUR)
    dispatcher.expire_idle_clients(lambda: main._light_audit_context(audit.correlation_id(), None, "playback_expiry"))
    drain()
    assert actions(light_calls) == ["dim", "restore"]
    queued = [r for r in rows(client) if r["action"] == "light.action_queued" and r["detail"]["request"] == "restore"]
    assert [r["detail"]["on_behalf_of"] for r in queued] == [
        {"kind": "playback_expiry", "id": "playback-expiry", "verified": 0}]


ALLOWED = "allowed_server_uuids: [server-ours]\n"


def from_server(event, server_uuid):
    body = payload(event)
    if server_uuid is not None:
        body["Server"] = {"title": "Plex", "uuid": server_uuid}
    return body


@pytest.fixture
def allow_list(registry, rooms_file):
    rooms_file.write_text(ROOMS_YAML + ALLOWED, encoding="utf-8")
    registry.reload()
    return registry


def unlisted_count():
    return REGISTRY.get_sample_value("plex_webhook_unlisted_server_events_total") or 0


def test_event_from_listed_server_is_dispatched(client, allow_list, light_calls):
    post(client, from_server("media.play", "server-ours"))
    drain()
    assert light_calls == [("dim", ["AA:BB"])]


@pytest.mark.parametrize("server_uuid", ["server-other", None])
def test_event_from_unlisted_server_is_recorded_but_not_dispatched(client, allow_list, light_calls, server_uuid):
    before = unlisted_count()
    response = post(client, from_server("media.play", server_uuid))
    drain()
    assert response.status_code == 200
    assert light_calls == []
    assert dispatcher._active_clients == {}
    assert unlisted_count() == before + 1
    assert client.conn.execute("SELECT event FROM events").fetchall() == [("media.play",)]
    assert "media.play" in client.log.read_text(encoding="utf-8")
    receipt = [r for r in rows(client) if r["action"] == "webhook.receipt"][-1]
    assert (receipt["outcome"], receipt["reason_code"]) == ("received", "server_not_allowed")


def test_unlisted_stop_does_not_restore(client, allow_list, light_calls):
    post(client, from_server("media.play", "server-ours"))
    post(client, from_server("media.stop", "server-other"))
    drain()
    assert light_calls == [("dim", ["AA:BB"])]


@pytest.mark.parametrize("server_uuid", ["server-anything", None])
def test_without_allow_list_nothing_changes(client, registry, light_calls, server_uuid):
    before = unlisted_count()
    post(client, from_server("media.play", server_uuid))
    drain()
    assert light_calls == [("dim", ["AA:BB"])]
    assert unlisted_count() == before
    receipt = [r for r in rows(client) if r["action"] == "webhook.receipt"][-1]
    assert (receipt["outcome"], receipt["reason_code"]) == ("received", "accepted")


@pytest.mark.parametrize("server", ["not-a-dict", {"uuid": 7}, {}])
def test_malformed_server_is_not_allowed(allow_list, server):
    assert not allow_list.server_allowed({**payload("media.play"), "Server": server})


@pytest.mark.parametrize("extra, path, code", [
    ("allowed_server_uuids: []", "allowed_server_uuids", "expected_nonempty_list"),
    ("allowed_server_uuids: server-ours", "allowed_server_uuids", "expected_nonempty_list"),
    ("allowed_server_uuids: [server-ours, 42]", "allowed_server_uuids[1]", "expected_nonempty_string"),
    ("allowed_server_uuids: ['  ']", "allowed_server_uuids[0]", "expected_nonempty_string"),
    ("playback_idle_minutes: 0", "playback_idle_minutes", "expected_integer"),
    ("playback_idle_minutes: true", "playback_idle_minutes", "expected_integer"),
    ("playback_idle_minutes: 90.5", "playback_idle_minutes", "expected_integer"),
    ("playback_idle_minutes: 10081", "playback_idle_minutes", "expected_integer"),
])
def test_invalid_options_are_rejected(registry, rooms_file, extra, path, code):
    before = registry._state
    rooms_file.write_text(ROOMS_YAML + extra + "\n", encoding="utf-8")
    with pytest.raises(RoomConfigError) as error:
        registry.reload()
    assert error.value.detail == [{"path": path, "code": code}]
    assert registry._state is before


def test_null_allow_list_means_unset(registry, rooms_file):
    rooms_file.write_text(ROOMS_YAML + "allowed_server_uuids: null\n", encoding="utf-8")
    registry.reload()
    assert registry.server_allowed(payload("media.play"))


def test_sweeper_expires_with_playback_expiry_context_and_survives_errors(monkeypatch):
    import asyncio

    import app.main as main

    calls, sleeps = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    def fake_expire(context_factory):
        calls.append(context_factory)
        if len(calls) == 1:
            raise RuntimeError("first sweep fails")

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(dispatcher, "expire_idle_clients", fake_expire)
    monkeypatch.setattr(main, "_light_audit_context", lambda correlation, event_id, actor: (event_id, actor))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._expire_idle_players())
    assert sleeps == [main.EXPIRY_SWEEP_SECONDS] * 3
    assert len(calls) == 2  # a failed sweep does not stop the next one
    assert calls[1]() == (None, "playback_expiry")
