import pytest
from prometheus_client import REGISTRY

from app import dispatcher
from app.rooms import RoomConfigError
from conftest import ROOMS_YAML, drain, payload, post, rows

MOVIES_ONLY = ROOMS_YAML.replace("    name: Living Room\n",
                                 "    name: Living Room\n    dim_media_types: [movie, ' Episode ']\n")
AUTOMATION = """
automations:
  - trigger: {room: living_room, event: dim}
    target_room: bedroom
    action: 40
"""
DIMMED = [("dim", ["AA:BB"]), ("dim", ["plug1"])]
RESTORED = [*DIMMED, ("restore", ["AA:BB"]), ("restore", ["plug1"])]


def event(name, media_type="movie", title="Living Room TV", uuid=None):
    body = payload(name, title, uuid)
    if media_type is None:
        del body["Metadata"]["type"]
    else:
        body["Metadata"]["type"] = media_type
    dispatcher.handle_event(body)
    drain()


@pytest.fixture
def movies_only(registry, rooms_file):
    rooms_file.write_text(MOVIES_ONLY + AUTOMATION, encoding="utf-8")
    registry.reload()
    return registry


def test_without_option_every_type_dims(registry, light_calls):
    event("media.play", "track")
    assert light_calls == [("dim", ["AA:BB"])]


@pytest.mark.parametrize("media_type", ["movie", "Movie", " MOVIE ", "episode"])
def test_listed_type_dims(movies_only, light_calls, media_type):
    event("media.play", media_type)
    assert light_calls == [("dim", ["AA:BB"]), ("dim", ["plug1"])]


@pytest.mark.parametrize("name", ["media.play", "media.resume"])
@pytest.mark.parametrize("media_type", ["track", "photo", "clip", None, 42])
def test_unlisted_type_is_ignored(movies_only, light_calls, name, media_type):
    event(name, media_type)
    assert light_calls == []
    assert dispatcher._active_clients == {}
    assert dispatcher._pending_rooms == set()
    assert dispatcher._action_executor.pending() == 0


def test_missing_metadata_is_ignored(movies_only, light_calls):
    body = payload("media.play")
    del body["Metadata"]
    dispatcher.handle_event(body)
    drain()
    assert light_calls == []


@pytest.mark.parametrize("stop_event", ["media.pause", "media.stop"])
@pytest.mark.parametrize("media_type", ["movie", "track", None])
def test_pause_or_stop_of_any_type_restores(movies_only, light_calls, stop_event, media_type):
    event("media.play", "movie")
    event(stop_event, media_type)
    assert [call[0] for call in light_calls][-1] == "restore"
    assert ("restore", ["AA:BB"]) in light_calls
    assert dispatcher._active_clients["living_room"] == {}


def test_stop_for_ignored_play_is_harmless(movies_only, light_calls):
    event("media.play", "track")
    event("media.stop", "track")
    assert light_calls == []
    assert dispatcher._active_clients == {"living_room": {}}


def test_film_then_music_ends_restored(movies_only, light_calls):
    event("media.play", "movie")
    event("media.stop", "movie")
    event("media.play", "track")
    assert light_calls == [("dim", ["AA:BB"]), ("dim", ["plug1"]), ("restore", ["AA:BB"]), ("restore", ["plug1"])]
    assert dispatcher._active_clients == {"living_room": {}}


def active_sessions(room="living_room"):
    return REGISTRY.get_sample_value("plex_dispatcher_room_active_sessions", {"room": room})


@pytest.mark.parametrize("name", ["media.play", "media.resume"])
@pytest.mark.parametrize("media_type", ["track", "photo", None, 42])
def test_filtered_play_right_after_film_restores_at_once(movies_only, light_calls, name, media_type):
    event("media.play", "movie", uuid="uuid-living")
    event(name, media_type, uuid="uuid-living")
    assert light_calls == RESTORED
    assert dispatcher._active_clients == {"living_room": {}}
    assert dispatcher._pending_rooms == set()
    assert active_sessions() == 0


def test_title_only_filtered_play_right_after_film_restores(movies_only, light_calls):
    event("media.play", "movie")
    event("media.play", "track")
    assert light_calls == RESTORED


def test_later_events_after_the_switch_dispatch_nothing_more(movies_only, light_calls, monkeypatch):
    event("media.play", "movie", uuid="uuid-living")
    event("media.play", "track", uuid="uuid-living")
    event("media.stop", "movie", uuid="uuid-living")  # a stop that arrives after the next item's play
    event("media.play", "track", uuid="uuid-living")
    event("media.stop", "track", uuid="uuid-living")
    monkeypatch.setattr(dispatcher, "_clock", lambda: float("inf"))
    dispatcher.expire_idle_clients()
    drain()
    assert light_calls == RESTORED


def test_film_after_the_switch_dims_again(movies_only, light_calls):
    for media_type in ("movie", "track", "movie"):
        event("media.play", media_type, uuid="uuid-living")
    assert light_calls == [*RESTORED, *DIMMED]
    assert active_sessions() == 1


def test_unlisted_player_switching_to_music_ends_its_own_film(movies_only, light_calls):
    event("media.play", "movie", uuid="synthetic-unlisted")
    event("media.play", "track", uuid="synthetic-unlisted")
    assert light_calls == RESTORED
    assert dispatcher._activating_uuids == {}


@pytest.mark.parametrize("film_uuid", ["uuid-living", "synthetic-unlisted"])
def test_filtered_play_from_another_device_sharing_the_title_changes_nothing(movies_only, light_calls, film_uuid):
    event("media.play", "movie", uuid=film_uuid)
    before = dict(dispatcher._active_clients["living_room"])
    event("media.play", "track", uuid="synthetic-other-device")
    assert light_calls == DIMMED
    assert dispatcher._active_clients["living_room"] == before
    assert active_sessions() == 1
    event("media.stop", "movie", uuid=film_uuid)
    assert light_calls == RESTORED


def test_filtered_play_with_records_but_no_active_entry_dispatches_nothing(movies_only, light_calls):
    # After a restart the dim's records survive with no player state: only a stop or pause restores then.
    dispatcher._pending_rooms.add("living_room")
    event("media.play", "track", uuid="uuid-living")
    assert light_calls == []
    assert dispatcher._active_clients == {}
    assert dispatcher._pending_rooms == {"living_room"}
    event("media.stop", "track", uuid="uuid-living")
    assert dispatcher._pending_rooms == set()


def test_room_without_the_option_stays_dimmed_from_film_to_music(movies_only, light_calls):
    event("media.play", "movie", title="Bedroom Apple TV", uuid="uuid-bed-tv")
    event("media.play", "track", title="Bedroom Apple TV", uuid="uuid-bed-tv")
    assert light_calls == [("dim", ["plug1"])]
    assert active_sessions("bedroom") == 1


def test_switch_restore_runs_automations_and_is_audited(client, movies_only, light_calls):
    for media_type in ("movie", "track"):
        body = payload("media.play", uuid="uuid-living")
        body["Metadata"]["type"] = media_type
        assert post(client, body).status_code == 200
        drain()
    assert light_calls == RESTORED
    queued = [r for r in rows(client) if r["action"] == "light.action_queued"]
    assert [(r["target_id"], r["detail"]["request"]) for r in queued] == [
        ("living_room", "dim"), ("bedroom", "dim"), ("living_room", "restore"), ("bedroom", "restore")]


def test_option_is_per_room(movies_only, light_calls):
    event("media.play", "track", title="Bedroom Apple TV")
    assert light_calls == [("dim", ["plug1"])]


@pytest.mark.parametrize("value, path, code", [
    ("movie", "rooms.living_room.dim_media_types", "expected_list"),
    ("{movie: true}", "rooms.living_room.dim_media_types", "expected_list"),
    ("[]", "rooms.living_room.dim_media_types", "expected_nonempty_list"),
    ("[movie, 42]", "rooms.living_room.dim_media_types[1]", "expected_nonempty_string"),
    ("[movie, '  ']", "rooms.living_room.dim_media_types[1]", "expected_nonempty_string"),
    ("[null]", "rooms.living_room.dim_media_types[0]", "expected_nonempty_string"),
])
def test_invalid_values_are_rejected(registry, rooms_file, value, path, code):
    before = registry._state
    rooms_file.write_text(ROOMS_YAML.replace("    name: Living Room\n",
                                             f"    name: Living Room\n    dim_media_types: {value}\n"),
                          encoding="utf-8")
    for operation in (registry.validate, registry.reload):
        with pytest.raises(RoomConfigError) as error:
            operation()
        assert error.value.detail == [{"path": path, "code": code}]
        assert registry._state is before


def test_null_means_unset(registry, rooms_file, light_calls):
    rooms_file.write_text(ROOMS_YAML.replace("    name: Living Room\n",
                                             "    name: Living Room\n    dim_media_types: null\n"),
                          encoding="utf-8")
    registry.reload()
    event("media.play", "track")
    assert light_calls == [("dim", ["AA:BB"])]


def test_ignored_event_is_stored_counted_and_audited(client, movies_only, light_calls):
    labels = {"event": "media.play", "player": "Living Room TV", "account": "known"}
    before = REGISTRY.get_sample_value("plex_webhook_events_total", labels) or 0
    body = payload("media.play")
    body["Metadata"]["type"] = "track"
    assert post(client, body).status_code == 200
    drain()
    assert light_calls == []
    assert dispatcher._active_clients == {}
    assert client.conn.execute("SELECT event, media_type FROM events").fetchall() == [("media.play", "track")]
    assert REGISTRY.get_sample_value("plex_webhook_events_total", labels) == before + 1
    receipt = [r for r in rows(client) if r["action"] == "webhook.receipt"][-1]
    assert (receipt["outcome"], receipt["reason_code"]) == ("received", "accepted")
    assert not [r for r in rows(client) if r["action"].startswith("light.")]
