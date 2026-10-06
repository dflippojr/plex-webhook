from prometheus_client import REGISTRY

from app import dispatcher
from conftest import drain, payload


def play(title="Living Room TV", uuid=None):
    dispatcher.handle_event(payload("media.play", title, uuid))
    drain()


def stop(title="Living Room TV", uuid=None, event="media.stop"):
    dispatcher.handle_event(payload(event, title, uuid))
    drain()


def test_single_client_dims_then_restores(registry, light_calls):
    play()
    assert light_calls == [("dim", ["AA:BB"])]
    stop()
    assert light_calls == [("dim", ["AA:BB"]), ("restore", ["AA:BB"])]


def test_pause_restores_and_resume_dims(registry, light_calls):
    play()
    stop(event="media.pause")
    dispatcher.handle_event(payload("media.resume"))
    drain()
    assert [c[0] for c in light_calls] == ["dim", "restore", "dim"]


def test_repeated_play_does_not_redim(registry, light_calls):
    play()
    play()
    assert light_calls == [("dim", ["AA:BB"])]


def test_two_clients_restore_only_after_last_stops(registry, light_calls):
    play("Bedroom Apple TV")
    play("Bedroom Phone", "uuid-bed-phone")
    assert light_calls == [("dim", ["plug1"])]
    stop("Bedroom Apple TV")
    assert light_calls == [("dim", ["plug1"])]
    stop("Bedroom Phone", "uuid-bed-phone")
    assert light_calls == [("dim", ["plug1"]), ("restore", ["plug1"])]


def test_rooms_are_independent(registry, light_calls):
    play()
    play("Bedroom Apple TV")
    assert sorted(light_calls) == [("dim", ["AA:BB"]), ("dim", ["plug1"])]


def test_unmapped_client_ignored(registry, light_calls):
    play("Kitchen iPad")
    stop("Kitchen iPad")
    assert light_calls == []
    assert dispatcher._active_clients == {}


def test_stop_or_pause_of_unknown_client_is_harmless(registry, light_calls):
    stop()
    stop(event="media.pause")
    assert light_calls == []
    play()
    stop("Living Room TV", "some-other-uuid")  # same room, but a client that never started
    assert light_calls == [("dim", ["AA:BB"])]


def test_other_events_and_empty_payload_ignored(registry, light_calls):
    dispatcher.handle_event(payload("media.scrobble"))
    dispatcher.handle_event(payload("library.new"))
    dispatcher.handle_event(None)
    dispatcher.handle_event({})
    drain()
    assert light_calls == []


def test_client_id_prefers_uuid_then_title():
    assert dispatcher._client_id({"Player": {"title": "TV"}}) == "TV"
    assert dispatcher._client_id({"Player": {"uuid": "u", "title": "TV"}}) == "u"
    assert dispatcher._client_id({}) == "unknown"


def test_gauges_and_counters(registry, light_calls):
    labels = {"room": "living_room"}
    before = REGISTRY.get_sample_value("plex_dispatcher_actions_total", {**labels, "action": "restore"}) or 0
    play()
    assert REGISTRY.get_sample_value("plex_dispatcher_room_active_sessions", labels) == 1
    stop()
    assert REGISTRY.get_sample_value("plex_dispatcher_room_active_sessions", labels) == 0
    assert REGISTRY.get_sample_value("plex_dispatcher_actions_total", {**labels, "action": "restore"}) == before + 1
