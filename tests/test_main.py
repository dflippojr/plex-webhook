import asyncio
import dataclasses
import json
import sqlite3
import types

import pytest
from starlette.exceptions import HTTPException as StarletteHTTPException

from conftest import ROOMS_YAML, drain, payload, post, rows


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_webhook_valid_payload_records_and_dispatches(client, light_calls):
    resp = post(client, payload("media.play"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "received", "event": "media.play"}
    assert client.conn.execute("SELECT event, player_title, title FROM events").fetchall() == [
        ("media.play", "Living Room TV", "A Film")
    ]
    assert json.loads(client.conn.execute("SELECT raw_payload FROM events").fetchone()[0]) == payload("media.play")
    drain()
    drain()
    assert light_calls == [("dim", ["AA:BB"])]


def test_webhook_received_at_always_has_microseconds(client, monkeypatch):
    import app.main as main

    class WholeSecond(main.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 18, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(main, "datetime", WholeSecond)
    assert post(client, payload("media.play")).status_code == 200
    expected = "2026-07-18T12:00:00.000000+00:00"
    assert not client.log.exists()
    assert client.conn.execute("SELECT received_at FROM events").fetchall() == [(expected,)]
    drain()
    drain()


def test_webhook_invalid_json_still_200_and_recorded(client, light_calls):
    resp = client.post("/webhook", data={"payload": "{not json"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "received", "event": None}
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    drain()
    assert light_calls == []


def test_webhook_without_payload_field(client):
    resp = client.post("/webhook", data={"other": "x"})
    assert resp.status_code == 200
    assert resp.json()["event"] is None
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_webhook_with_two_attachments_stores_both_in_order(client):
    resp = client.post(
        "/webhook",
        data={"payload": json.dumps(payload("media.stop"))},
        files=[("thumb", ("t.jpg", b"jpgbytes", "image/jpeg")), ("art", ("a.png", b"pngbytes", "image/png"))],
    )
    assert resp.status_code == 200
    (stored,) = client.conn.execute("SELECT attachments FROM events").fetchone()
    assert json.loads(stored) == [{"field": "thumb", "filename": "t.jpg", "content_type": "image/jpeg"},
                                  {"field": "art", "filename": "a.png", "content_type": "image/png"}]


def test_attachment_metadata_is_stored_in_sqlite(client):
    resp = client.post(
        "/webhook",
        data={"payload": json.dumps(payload("media.stop"))},
        files={"thumb": ("t.jpg", b"jpgbytes", "image/jpeg")},
    )
    assert resp.status_code == 200
    (stored,) = client.conn.execute("SELECT attachments FROM events").fetchone()
    assert json.loads(stored) == [{"field": "thumb", "filename": "t.jpg", "content_type": "image/jpeg"}]


def test_unpaired_surrogate_in_attachment_filename_is_stored(memory_db):
    from app.db import insert_event

    insert_event(memory_db, "t", None, None, "null",
                 attachments=[{"field": "thumb", "filename": "a\ud800.jpg", "content_type": "image/jpeg"}])
    (stored,) = memory_db.execute("SELECT attachments FROM events").fetchone()
    assert json.loads(stored)[0]["filename"] == "a\ud800.jpg"


def test_no_event_log_is_written_and_every_receipt_has_its_row(client, registry, rooms_file, tmp_path):
    rooms_file.write_text(ROOMS_YAML + "allowed_server_uuids: [server-ours]\n", encoding="utf-8")
    registry.reload()
    listed, unlisted = payload("media.play"), payload("media.play")
    listed["Server"], unlisted["Server"] = {"uuid": "server-ours"}, {"uuid": "server-other"}
    for request in ({"payload": json.dumps(listed)}, {"payload": "{not json"},
                    {"other": "x"}, {"payload": json.dumps(unlisted)}):
        assert client.post("/webhook", data=request).status_code == 200
    drain()
    drain()
    for directory in (tmp_path, client.service.settings.db_path.parent):
        assert not (directory / "events.jsonl").exists()
    receipts = [r for r in rows(client) if r["action"] == "webhook.receipt"]
    assert [r["reason_code"] for r in receipts] == ["accepted", "invalid_json", "missing_payload", "server_not_allowed"]
    stored = [row[0] for row in client.conn.execute("SELECT id FROM events ORDER BY id")]
    assert [(r["target_kind"], r["target_id"]) for r in receipts] == [("event", str(i)) for i in stored]
    assert len(stored) == 4


def _event_series(service):
    return {tuple(sample.labels[name] for name in ("event", "player", "account"))
            for metric in service.metrics.registry.collect() if metric.name == "plex_webhook_events"
            for sample in metric.samples if sample.name == "plex_webhook_events_total"}


def test_webhook_increments_metrics(client):
    labels = {"event": "media.pause", "player": "Living Room TV", "account": "known"}
    registry = client.service.metrics.registry
    before = registry.get_sample_value("plex_webhook_events_total", labels) or 0
    post(client, payload("media.pause", title="living room tv "))
    assert registry.get_sample_value("plex_webhook_events_total", labels) == before + 1
    assert b"plex_webhook_events_total" in client.get("/metrics").content


def test_event_labels_map_unknown_values_to_other(service):
    assert service.event_labels("media.play", " BEDROOM apple tv", "unknown", "dan") == (
        "media.play", "Bedroom Apple TV", "known")
    assert service.event_labels("media.bogus", "Stranger TV", "unknown", "unknown") == ("other", "other", "other")
    assert service.event_labels("unknown", "unknown", "unknown", "unknown") == ("unknown", "unknown", "other")
    # Resolved like dispatch: uuid first, so a renamed or untitled player keeps its room's label.
    assert service.event_labels("media.play", "Renamed TV", "uuid-living", "dan")[1] == "Living Room TV"
    assert service.event_labels("media.play", "unknown", "uuid-bed-tv", "dan")[1] == "Bedroom Apple TV"
    assert service.event_labels("media.play", "unknown", "uuid-stranger", "dan")[1] == "unknown"
    labels = {service.event_labels(f"evt.{i}", f"Player {i}", f"uuid-{i}", f"user{i}") for i in range(1000)}
    assert labels == {("other", "other", "known")}


NULL_TITLE = ROOMS_YAML.replace("      - title: Living Room TV\n        uuid: uuid-living\n",
                                "      - title: null\n        uuid: uuid-living\n")


def _total(client, event, player, account):
    return client.service.metrics.registry.get_sample_value(
        "plex_webhook_events_total", {"event": event, "player": player, "account": account}) or 0


def test_uuid_match_with_new_title_counts_under_its_room(client, light_calls):
    from app.db import labeled_event_counts

    other, known = _total(client, "media.play", "other", "known"), _total(client, "media.play", "Living Room TV", "known")
    assert post(client, payload("media.play", title="Renamed TV", uuid="uuid-living")).status_code == 200
    drain()
    assert light_calls == [("dim", ["AA:BB"])]
    assert _total(client, "media.play", "Living Room TV", "known") == known + 1
    assert _total(client, "media.play", "other", "known") == other
    reseeded = labeled_event_counts(client.conn, client.service.event_labels)
    assert reseeded == {("media.play", "Living Room TV", "known"): 1}


def test_null_title_client_counts_under_the_room_key(client, rooms_file, light_calls):
    from app.db import labeled_event_counts

    assert NULL_TITLE != ROOMS_YAML
    rooms_file.write_text(NULL_TITLE, encoding="utf-8")
    assert client.post("/rooms/reload").status_code == 200
    other, room = _total(client, "media.play", "other", "known"), _total(client, "media.play", "living_room", "known")
    assert post(client, payload("media.play", title="Living Room TV", uuid="uuid-living")).status_code == 200
    drain()
    assert light_calls == [("dim", ["AA:BB"])]
    assert _total(client, "media.play", "living_room", "known") == room + 1
    assert _total(client, "media.play", "other", "known") == other
    reseeded = labeled_event_counts(client.conn, client.service.event_labels)
    assert reseeded == {("media.play", "living_room", "known"): 1}


def test_webhook_labels_stay_bounded_for_many_distinct_values(client):
    from app.service import PLEX_EVENTS

    for i in range(40):
        body = payload(f"synthetic.{i}", title=f"Synthetic Player {i}", account=f"synthetic-user-{i}")
        assert post(client, body).status_code == 200
    post(client, payload("media.stop"))
    client.post("/webhook", data={"payload": "{not json"})
    series = _event_series(client.service)
    assert not any("synthetic" in value.lower() for labels in series for value in labels)
    event_values = PLEX_EVENTS | {"other", "unknown"}
    assert {labels[0] for labels in series} <= event_values
    # Room keys appear when a test configures a client without a title.
    rooms = {"Living Room TV", "Bedroom Apple TV", "living_room", "bedroom"}
    assert {labels[1] for labels in series} <= rooms | {"other", "unknown"}
    assert {labels[2] for labels in series} <= {"known", "other"}
    assert ("other", "other", "known") in series
    assert ("unknown", "unknown", "other") in series


def test_clients_lists_seen_players(client):
    post(client, payload("media.play", "Living Room TV", "uuid-living"))
    post(client, payload("media.stop", "Living Room TV", "uuid-living"))
    clients = client.get("/clients").json()["clients"]
    assert len(clients) == 1
    assert clients[0]["title"] == "Living Room TV"
    assert clients[0]["uuid"] == "uuid-living"
    assert clients[0]["event_count"] == 2


def test_clients_route_docstring_is_the_description():
    import app.main as main
    assert main.clients.__doc__ == "Distinct Plex clients seen so far, to help fill in the local config/rooms.yaml."


def test_rooms_and_reload(client, rooms_file):
    assert set(client.get("/rooms").json()["rooms"]) == {"living_room", "bedroom"}
    rooms_file.write_text("rooms:\n  den:\n    plex_clients: [{title: x}]\n    lights: []\n", encoding="utf-8")
    resp = client.post("/rooms/reload")
    assert resp.json() == {"status": "reloaded", "rooms": ["den"]}
    assert list(client.get("/rooms").json()["rooms"]) == ["den"]


def test_validate_does_not_publish_or_dispatch(client, registry, rooms_file, monkeypatch, light_calls, playback):
    post(client, payload("media.play", uuid="uuid-living"))
    drain()
    # Rooms, indexes, automations and options are published as one reference, so an unchanged
    # rooms mapping means nothing was published.
    before = registry.rooms
    active = {key: dict(value) for key, value in playback.active_clients.items()}
    metrics = list(playback.metrics.room_active_sessions.collect())[0].samples
    calls = list(light_calls)
    def unexpected():
        pytest.fail("validation must not initialize gauges")
    original_init = playback.init_room_gauges
    monkeypatch.setattr(playback, "init_room_gauges", unexpected)
    rooms_file.write_text("rooms: {den: {plex_clients: [{title: x}]}}", encoding="utf-8")
    assert client.post("/rooms/validate").json() == {"status": "valid", "rooms": ["den"]}
    assert registry.rooms is before
    assert playback.active_clients == active
    assert list(playback.metrics.room_active_sessions.collect())[0].samples == metrics
    assert light_calls == calls
    monkeypatch.setattr(playback, "init_room_gauges", original_init)
    assert client.post("/rooms/reload").json() == {"status": "reloaded", "rooms": ["den"]}


@pytest.mark.parametrize("endpoint", ["validate", "reload"])
@pytest.mark.parametrize("source", [
    "rooms: [private-value", "rooms: {den: {plex_clients: [42]}}",
    "rooms: {den: {}, den: {}}",
    "rooms: {a: {plex_clients: [{uuid: private-value}]}, b: {plex_clients: [{uuid: private-value}]}}",
    "rooms: {a: {plex_clients: [{title: Private-Value}]}, b: {plex_clients: [{title: ' private-value '}]}}",
])
def test_invalid_room_endpoints_preserve_dispatch(client, registry, rooms_file, light_calls, monkeypatch, endpoint, source, playback):
    post(client, payload("media.play", uuid="uuid-living"))
    drain()
    before = registry.rooms
    active = {key: dict(value) for key, value in playback.active_clients.items()}
    metrics = list(playback.metrics.room_active_sessions.collect())[0].samples
    calls = list(light_calls)
    def unexpected():
        pytest.fail("failed candidates must not initialize gauges")
    monkeypatch.setattr(playback, "init_room_gauges", unexpected)
    rooms_file.write_text(source, encoding="utf-8")
    resp = client.post(f"/rooms/{endpoint}")
    assert resp.status_code == 422
    assert all(set(error) == {"path", "code"} for error in resp.json()["detail"])
    assert "private-value" not in resp.text.lower()
    assert registry.rooms is before
    assert playback.active_clients == active
    assert list(playback.metrics.room_active_sessions.collect())[0].samples == metrics
    assert light_calls == calls
    post(client, payload("media.stop", uuid="uuid-living"))
    drain()
    assert light_calls[-1] == ("restore", ["AA:BB"])


@pytest.mark.parametrize("endpoint", ["validate", "reload"])
@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_unavailable_room_endpoints(client, registry, playback, monkeypatch, endpoint, failure):
    from pathlib import Path

    before = registry.rooms
    def fail(*args, **kwargs):
        raise failure("private-value")
    def unexpected():
        pytest.fail("unavailable config must not initialize gauges")
    monkeypatch.setattr(Path, "read_text", fail)
    monkeypatch.setattr(playback, "init_room_gauges", unexpected)
    resp = client.post(f"/rooms/{endpoint}")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Rooms configuration unavailable"}
    assert registry.rooms is before


@pytest.fixture
def invalid_startup_rooms(rooms_file):
    """Requested before ``client``, so the app starts with this invalid rooms file."""
    rooms_file.write_text("rooms: [private-value", encoding="utf-8")
    return rooms_file


def test_invalid_startup_still_captures_webhook(invalid_startup_rooms, client, light_calls):
    """A rooms file that is invalid when the app starts leaves empty mappings; capture still works."""
    service = client.service
    assert service.registry.rooms == {}
    assert (service.registry.load_outcome, service.registry.load_reason) == ("rejected", "invalid_config")
    assert post(client, payload("media.play")).status_code == 200
    assert client.conn.execute("SELECT event FROM events").fetchall() == [("media.play",)]
    drain(service)
    assert light_calls == []


def test_counters_and_timestamp_seeded_from_sqlite_on_startup(tmp_path, settings, make_service):
    import app.db

    db_path = tmp_path / "seed.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(app.db.SCHEMA)
    for event in ("media.play", "media.play", "media.stop"):
        app.db.insert_event(conn, "2024-05-01T12:00:00+00:00", event, payload(event, "Living Room TV"), "{}")
    app.db.insert_event(conn, "2024-05-01T11:00:00+00:00", "media.play", payload("media.play", "Seed TV"), "{}")
    # Renamed in Plex since rooms.yaml was written: re-seeded under its room, like live counting.
    app.db.insert_event(conn, "2024-05-01T10:00:00+00:00", "media.play",
                        payload("media.play", "Renamed TV", uuid="uuid-living"), "{}")
    # Many distinct synthetic values collapse into a handful of series.
    conn.executemany(
        "INSERT INTO events (received_at, event, player_title, account_title, raw_payload) VALUES (?,?,?,?,?)",
        (("2024-04-01T00:00:00+00:00", f"synthetic.{i}", f"Synthetic {i}", f"user{i}", "{}") for i in range(5000)),
    )
    conn.commit()
    conn.close()

    # A service started on the seeded database: its fresh collectors are seeded from the stored history.
    service = make_service(dataclasses.replace(settings, db_path=db_path))
    registry = service.metrics.registry
    labels = {"player": "Living Room TV", "account": "known"}
    assert registry.get_sample_value("plex_webhook_events_total", {"event": "media.play", **labels}) == 3
    assert registry.get_sample_value("plex_webhook_events_total", {"event": "media.stop", **labels}) == 1
    other_player = {"event": "media.play", "player": "other", "account": "known"}
    assert registry.get_sample_value("plex_webhook_events_total", other_player) == 1
    other_all = {"event": "other", "player": "other", "account": "known"}
    assert registry.get_sample_value("plex_webhook_events_total", other_all) == 5000
    assert len(_event_series(service)) == 4
    assert registry.get_sample_value("plex_webhook_last_event_timestamp_seconds") == 1714564800.0


def _stored(client):
    return (client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            client.log.exists())


def _rejected(client, reason):
    return client.service.metrics.registry.get_sample_value(
        "plex_webhook_rejected_requests_total", {"reason": reason}) or 0


def test_oversized_content_length_is_413_and_stores_nothing(client, light_calls):
    before = _rejected(client, "body_too_large")
    resp = client.post("/webhook", data={"payload": "x" * (3 * 1024 * 1024)})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected(client, "body_too_large") == before + 1
    drain()
    assert light_calls == []


def test_body_limiter_passes_non_request_messages():
    import app.main as main

    # The disconnect carries a body only so that counting it would trip the cap.
    disconnect = {"type": "http.disconnect", "body": b"x" * 100}
    messages = [{"type": "http.request", "body": b"x" * 6}, disconnect,
                {"type": "http.request", "body": b"x" * 4}, {"type": "http.request", "body": b"x"}]
    source = iter(messages)

    async def receive():
        return next(source)

    request = types.SimpleNamespace(_receive=receive)
    main._limit_streamed_body(request, 10)

    async def read():
        return [await request._receive() for _ in range(3)]

    assert asyncio.run(read()) == messages[:3]
    with pytest.raises(main._BodyTooLarge):  # the cap still counts the request messages
        asyncio.run(request._receive())


def test_streamed_body_over_cap_without_content_length_is_413(client, monkeypatch):
    service = client.service
    monkeypatch.setattr(service, "settings", dataclasses.replace(service.settings, webhook_max_body_bytes=1024))

    def chunks():
        yield b"payload=" + b"x" * 600
        yield b"y" * 600

    resp = client.post("/webhook", content=chunks(),
                       headers={"content-type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)


def test_too_many_fields_is_413_and_stores_nothing(client):
    before = _rejected(client, "too_many_fields")
    data = {"payload": json.dumps(payload("media.play"))}
    data.update({f"extra{i}": "v" for i in range(20)})
    resp = client.post("/webhook", data=data, files={"thumb": ("t.jpg", b"x", "image/jpeg")})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected(client, "too_many_fields") == before + 1


def test_too_many_files_is_413_and_stores_nothing(client):
    before = _rejected(client, "too_many_files")
    files = [(f"f{i}", (f"t{i}.jpg", b"x", "image/jpeg")) for i in range(5)]
    resp = client.post("/webhook", data={"payload": json.dumps(payload("media.play"))}, files=files)
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected(client, "too_many_files") == before + 1


def test_oversized_field_is_413_and_stores_nothing(client):
    before = _rejected(client, "field_too_large")
    resp = client.post("/webhook", data={"payload": "x" * (600 * 1024)}, files={"t": ("t.jpg", b"x", "image/jpeg")})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected(client, "field_too_large") == before + 1


def test_malformed_content_length_is_400(client):
    resp = client.post("/webhook", content=b"payload=x",
                       headers={"content-type": "application/x-www-form-urlencoded", "content-length": "abc"})
    assert resp.status_code == 400
    assert _stored(client) == (0, False)


def test_large_stored_payload_is_truncated_but_still_dispatched(client, light_calls, monkeypatch):
    service = client.service
    monkeypatch.setattr(service, "settings", dataclasses.replace(service.settings, raw_payload_max_bytes=300))
    body = payload("media.play")
    body["Metadata"]["summary"] = "é" * 2000
    assert post(client, body).status_code == 200
    raw, truncated, title = client.conn.execute("SELECT raw_payload, raw_truncated, title FROM events").fetchone()
    assert truncated == 1 and len(raw.encode("utf-8")) <= 300 and title == "A Film"
    assert json.dumps(body).startswith(raw)
    drain()
    drain()
    assert light_calls == [("dim", ["AA:BB"])]


def test_normal_payload_is_not_marked_truncated(client):
    post(client, payload("media.play"))
    assert client.conn.execute("SELECT raw_truncated FROM events").fetchone() == (0,)
    assert json.loads(client.conn.execute("SELECT raw_payload FROM events").fetchone()[0]) == payload("media.play")


def test_existing_database_gains_truncation_column(tmp_path):
    from app import storage

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, event TEXT, "
                     "account_title TEXT, player_title TEXT, player_uuid TEXT, media_type TEXT, title TEXT, "
                     "grandparent_title TEXT, rating_key TEXT, raw_payload TEXT NOT NULL)")
        conn.execute("INSERT INTO events (received_at, raw_payload) VALUES ('t', '{}')")
    conn = storage.open_database(path)  # what service startup runs on the file
    try:
        assert conn.execute("SELECT raw_truncated FROM events").fetchall() == [(0,)]
    finally:
        conn.close()


def test_existing_database_gains_attachments_column(tmp_path):
    import app.db as appdb
    from app import storage

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, event TEXT, "
                     "account_title TEXT, player_title TEXT, player_uuid TEXT, media_type TEXT, title TEXT, "
                     "grandparent_title TEXT, rating_key TEXT, raw_payload TEXT NOT NULL, "
                     "raw_truncated INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO events (received_at, raw_payload) VALUES ('t', '{}')")
    conn = storage.open_database(path)  # what service startup runs on the file
    try:
        # A row stored before the column existed has unknown attachments, not "none".
        assert conn.execute("SELECT attachments FROM events").fetchall() == [(None,)]
        appdb.insert_event(conn, "t2", "media.play", None, "{}", attachments=[])
        assert conn.execute("SELECT attachments FROM events ORDER BY id").fetchall() == [(None,), ("[]",)]
    finally:
        conn.close()


def test_urlencoded_oversized_field_is_413_and_stores_nothing(client):
    before = _rejected(client, "field_too_large")
    resp = client.post("/webhook", data={"payload": "x" * (600 * 1024)})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected(client, "field_too_large") == before + 1


def _hostile_post(client, raw):
    resp = client.post("/webhook", files={"payload": (None, raw)})
    assert resp.status_code == 200
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert not client.log.exists()
    return rows(client)[0]


@pytest.mark.parametrize("raw", [
    '{"event":"media.play","Player":{"title":"\\ud800"}}',
    '{"event":"media.play","Account":{"title":"\\udfff"}}',
    '{"event":"\\ud800"}',
    '{"event":"media.play","Metadata":{"title":"a\\ud800b"}}',
    '{"event":"media.play","Metadata":{"ratingKey":18446744073709551616}}',
    '{"event":"media.play","Metadata":{"type":-9223372036854775809}}',
])
def test_unstorable_payload_is_rejected_not_500(client, light_calls, raw):
    receipt = _hostile_post(client, raw)
    assert (receipt["outcome"], receipt["reason_code"]) == ("rejected", "invalid_payload")
    drain()
    assert light_calls == []


def test_signed_64_bit_extremes_are_still_stored(client):
    body = {"event": "library.new", "Metadata": {"ratingKey": 2 ** 63 - 1, "title": -(2 ** 63)}}
    assert post(client, body).status_code == 200
    assert client.conn.execute("SELECT rating_key, title FROM events").fetchone() == (str(2 ** 63 - 1), str(-(2 ** 63)))

def _webhook_metrics(client):
    return {(sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for metric in client.service.metrics.registry.collect() for sample in metric.samples
            if sample.name.startswith("plex_webhook_") and not sample.name.endswith("_created")}


def _assert_invalid_form(client, send):
    before = _webhook_metrics(client)
    resp = send()
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Invalid webhook form"}
    assert resp.headers["content-type"] == "application/json"
    assert _stored(client) == (0, False)
    assert [(r["source"], r["action"], r["outcome"], r["reason_code"], r["changed_fields"]) for r in rows(client)] == [
        ("webhook", "webhook.receipt", "rejected", "invalid_form", {})
    ]
    assert _webhook_metrics(client) == before


@pytest.mark.parametrize("declared", ["abc", "-1", "1.5", "1e3"])
def test_non_digit_content_length_is_invalid_form(client, declared):
    _assert_invalid_form(client, lambda: client.post("/webhook", content=b"payload=x", headers={
        "content-type": "application/x-www-form-urlencoded", "content-length": declared}))


@pytest.mark.parametrize("content_type,body", [
    ("multipart/form-data", b"payload=x"),
    ("multipart/form-data; boundary=abc", b"not a multipart body"),
    ("multipart/form-data; boundary=abc", b"--abc\r\nContent-Disposition: form-data\r\n\r\nx\r\n--abc--\r\n"),
])
def test_malformed_multipart_body_is_invalid_form(client, content_type, body):
    _assert_invalid_form(client, lambda: client.post("/webhook", content=body, headers={"content-type": content_type}))


@pytest.mark.parametrize("error", [
    StarletteHTTPException(status_code=400, detail="Unparseable form"),
    RuntimeError("parser failure"),
])
def test_form_parser_errors_are_invalid_form(client, monkeypatch, error):
    from starlette.requests import Request

    async def fail_form(self, **kwargs):
        raise error

    monkeypatch.setattr(Request, "form", fail_form)
    _assert_invalid_form(client, lambda: post(client, payload("media.play")))


# One hostile value for every payload field insert_event stores.
UNSTORABLE_FIELDS = [
    ("Account", "title", "\ud800"),
    ("Player", "title", "\ud800"),
    ("Player", "uuid", "\udfff"),
    ("Metadata", "type", "\ud800"),
    ("Metadata", "type", 2 ** 63),
    ("Metadata", "title", "a\udfffb"),
    ("Metadata", "title", -(2 ** 63) - 1),
    ("Metadata", "grandparentTitle", "\ud800"),
    ("Metadata", "grandparentTitle", 2 ** 64),
    ("Metadata", "ratingKey", "\udfff"),
    ("Metadata", "ratingKey", 2 ** 63),
]


@pytest.mark.parametrize("group,name,value", UNSTORABLE_FIELDS)
def test_unstorable_value_in_each_stored_field_is_invalid_payload(client, light_calls, group, name, value):
    body = payload("media.play")
    body[group][name] = value
    before = _webhook_metrics(client)
    resp = client.post("/webhook", files={"payload": (None, json.dumps(body))})
    assert (resp.status_code, resp.json()) == (200, {"status": "received", "event": None})
    receipt = rows(client)
    assert [(r["outcome"], r["reason_code"]) for r in receipt] == [("rejected", "invalid_payload")]
    assert client.conn.execute(
        "SELECT event, account_title, player_title, player_uuid, media_type, title, grandparent_title, rating_key, "
        "raw_payload, raw_truncated FROM events").fetchall() == [
        (None, None, None, None, None, None, None, None, json.dumps(body), 0)
    ]
    assert client.conn.execute("SELECT attachments FROM events").fetchall() == [("[]",)]
    after = _webhook_metrics(client)
    events = ("plex_webhook_events_total", (("account", "other"), ("event", "unknown"), ("player", "unknown")))
    assert after.pop(events) == before.pop(events, 0) + 1
    after.pop(("plex_webhook_last_event_timestamp_seconds", ()))
    before.pop(("plex_webhook_last_event_timestamp_seconds", ()), None)
    assert after == before
    drain()
    assert light_calls == []


@pytest.mark.parametrize("group,name,value,outcome", [
    ("Account", "title", 7, "invalid_payload"),
    ("Account", "title", True, "invalid_payload"),
    ("Player", "title", 1.5, "invalid_payload"),
    ("Player", "uuid", ["x"], "invalid_payload"),
    ("Metadata", "type", {"a": 1}, "invalid_payload"),
    ("Metadata", "grandparentTitle", ["x"], "invalid_payload"),
    ("Metadata", "ratingKey", True, "accepted"),
    ("Metadata", "title", 1.5, "accepted"),
    ("Metadata", "duration", ["x"], "accepted"),
])
def test_stored_field_type_rules(client, group, name, value, outcome):
    body = payload("library.new")
    body[group][name] = value
    assert post(client, body).status_code == 200
    assert [r["reason_code"] for r in rows(client)] == [outcome]


def test_unstorable_cases_cover_every_stored_field():
    from app.db import STORED_FIELDS

    assert {(group, name) for group, name, _ in UNSTORABLE_FIELDS} == {
        (group, name) for group, names in STORED_FIELDS for name in names}


def test_each_stored_field_lands_in_its_column(client):
    body = {"event": "media.scrobble", "Account": {"title": "acct"}, "Player": {"title": "player", "uuid": "uuid-1"},
            "Metadata": {"type": "episode", "title": "Pilot", "grandparentTitle": "Show", "ratingKey": 7}}
    assert post(client, body).status_code == 200
    assert client.conn.execute(
        "SELECT event, account_title, player_title, player_uuid, media_type, title, grandparent_title, rating_key "
        "FROM events").fetchall() == [("media.scrobble", "acct", "player", "uuid-1", "episode", "Pilot", "Show", "7")]



@pytest.mark.parametrize("metadata", ['"duration":1' + "0" * 400, '"duration":1000,"viewOffset":1' + "0" * 400])
def test_huge_integer_duration_falls_back_to_idle_expiry(client, light_calls, metadata):
    raw = '{"event":"media.play","Player":{"title":"Living Room TV"},"Metadata":{%s}}' % metadata
    receipt = _hostile_post(client, raw)
    assert (receipt["outcome"], receipt["reason_code"]) == ("received", "accepted")
    drain()
    drain()
    assert light_calls == [("dim", ["AA:BB"])]


def test_deeply_nested_json_is_never_500(client, light_calls):
    receipt = _hostile_post(client, '{"event":"media.play","x":' + "[" * 5000 + "]" * 5000 + "}")
    # Interpreters that cannot parse this depth reject it; newer ones store it like any payload.
    assert (receipt["outcome"], receipt["reason_code"]) in {("rejected", "invalid_json"), ("received", "accepted")}


def test_nesting_beyond_the_recursion_limit_is_invalid_json(client, light_calls, monkeypatch):
    import app.main as main

    real_loads = json.loads

    def too_deep(text, *args, **kwargs):
        if text == '{"event":"media.play"}':
            raise RecursionError
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(main.json, "loads", too_deep)
    receipt = _hostile_post(client, '{"event":"media.play"}')
    assert (receipt["outcome"], receipt["reason_code"]) == ("rejected", "invalid_json")
    drain()
    assert light_calls == []


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_tokens_are_invalid_json(client, light_calls, token):
    raw = ('{"event":"media.play","Account":{"title":"dan"},"Player":{"title":"Living Room TV","uuid":"uuid-living"},'
           '"Metadata":{"type":"movie","duration":%s,"ratingKey":"42"}}' % token)
    receipt = _hostile_post(client, raw)
    assert (receipt["action"], receipt["outcome"], receipt["reason_code"]) == ("webhook.receipt", "rejected", "invalid_json")
    assert client.conn.execute("SELECT raw_payload FROM events").fetchone()[0] == "null"
    drain()
    drain()
    assert light_calls == []
