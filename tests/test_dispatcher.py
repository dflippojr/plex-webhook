from app import dispatcher
from conftest import drain, payload


def test_other_events_and_empty_payload_ignored(registry, light_calls, playback):
    playback.handle_event(payload("media.scrobble"))
    playback.handle_event(payload("library.new"))
    playback.handle_event(None)
    playback.handle_event({})
    drain()
    assert light_calls == []


def test_client_id_prefers_uuid_then_title():
    assert dispatcher._client_id({"Player": {"title": "TV"}}) == "TV"
    assert dispatcher._client_id({"Player": {"uuid": "u", "title": "TV"}}) == "u"
    assert dispatcher._client_id({}) == "unknown"
