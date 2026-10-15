from app import dispatcher
from conftest import drain, payload


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
