import importlib
import json
import sqlite3

import pytest
from prometheus_client import REGISTRY
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
    assert json.loads(client.log.read_text().splitlines()[0])["event"] == "media.play"
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
    assert json.loads(client.log.read_text().splitlines()[0])["received_at"] == expected
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


def test_webhook_with_attachment_is_logged(client):
    resp = client.post(
        "/webhook",
        data={"payload": json.dumps(payload("media.stop"))},
        files={"thumb": ("t.jpg", b"jpgbytes", "image/jpeg")},
    )
    assert resp.status_code == 200
    record = json.loads(client.log.read_text().splitlines()[0])
    assert record["attachments"] == [{"field": "thumb", "filename": "t.jpg", "content_type": "image/jpeg"}]


def _event_series():
    return {tuple(sample.labels[name] for name in ("event", "player", "account"))
            for metric in REGISTRY.collect() if metric.name == "plex_webhook_events"
            for sample in metric.samples if sample.name == "plex_webhook_events_total"}


def test_webhook_increments_metrics(client):
    labels = {"event": "media.pause", "player": "Living Room TV", "account": "known"}
    before = REGISTRY.get_sample_value("plex_webhook_events_total", labels) or 0
    post(client, payload("media.pause", title="living room tv "))
    assert REGISTRY.get_sample_value("plex_webhook_events_total", labels) == before + 1
    assert b"plex_webhook_events_total" in client.get("/metrics").content


def test_event_labels_map_unknown_values_to_other(registry):
    import app.main as main

    assert main._event_labels("media.play", " BEDROOM apple tv", "unknown", "dan") == (
        "media.play", "Bedroom Apple TV", "known")
    assert main._event_labels("media.bogus", "Stranger TV", "unknown", "unknown") == ("other", "other", "other")
    assert main._event_labels("unknown", "unknown", "unknown", "unknown") == ("unknown", "unknown", "other")
    # Resolved like dispatch: uuid first, so a renamed or untitled player keeps its room's label.
    assert main._event_labels("media.play", "Renamed TV", "uuid-living", "dan")[1] == "Living Room TV"
    assert main._event_labels("media.play", "unknown", "uuid-bed-tv", "dan")[1] == "Bedroom Apple TV"
    assert main._event_labels("media.play", "unknown", "uuid-stranger", "dan")[1] == "unknown"
    labels = {main._event_labels(f"evt.{i}", f"Player {i}", f"uuid-{i}", f"user{i}") for i in range(1000)}
    assert labels == {("other", "other", "known")}


NULL_TITLE = ROOMS_YAML.replace("      - title: Living Room TV\n        uuid: uuid-living\n",
                                "      - title: null\n        uuid: uuid-living\n")


def _total(event, player, account):
    return REGISTRY.get_sample_value("plex_webhook_events_total",
                                     {"event": event, "player": player, "account": account}) or 0


def test_uuid_match_with_new_title_counts_under_its_room(client, light_calls):
    import app.main as main

    other, known = _total("media.play", "other", "known"), _total("media.play", "Living Room TV", "known")
    assert post(client, payload("media.play", title="Renamed TV", uuid="uuid-living")).status_code == 200
    drain()
    assert light_calls == [("dim", ["AA:BB"])]
    assert _total("media.play", "Living Room TV", "known") == known + 1
    assert _total("media.play", "other", "known") == other
    reseeded = main.labeled_event_counts(client.conn, main._event_labels)
    assert reseeded == {("media.play", "Living Room TV", "known"): 1}


def test_null_title_client_counts_under_the_room_key(client, rooms_file, light_calls):
    import app.main as main

    assert NULL_TITLE != ROOMS_YAML
    rooms_file.write_text(NULL_TITLE, encoding="utf-8")
    assert client.post("/rooms/reload").status_code == 200
    other, room = _total("media.play", "other", "known"), _total("media.play", "living_room", "known")
    assert post(client, payload("media.play", title="Living Room TV", uuid="uuid-living")).status_code == 200
    drain()
    assert light_calls == [("dim", ["AA:BB"])]
    assert _total("media.play", "living_room", "known") == room + 1
    assert _total("media.play", "other", "known") == other
    reseeded = main.labeled_event_counts(client.conn, main._event_labels)
    assert reseeded == {("media.play", "living_room", "known"): 1}


def test_webhook_labels_stay_bounded_for_many_distinct_values(client):
    import app.main as main

    for i in range(40):
        body = payload(f"synthetic.{i}", title=f"Synthetic Player {i}", account=f"synthetic-user-{i}")
        assert post(client, body).status_code == 200
    post(client, payload("media.stop"))
    client.post("/webhook", data={"payload": "{not json"})
    series = _event_series()
    assert not any("synthetic" in value.lower() for labels in series for value in labels)
    event_values = main.PLEX_EVENTS | {"other", "unknown"}
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


def test_validate_does_not_publish_or_dispatch(client, registry, rooms_file, monkeypatch, light_calls):
    from app import dispatcher

    post(client, payload("media.play", uuid="uuid-living"))
    drain()
    before = registry._state
    active = {key: dict(value) for key, value in dispatcher._active_clients.items()}
    metrics = list(dispatcher.ROOM_ACTIVE_SESSIONS.collect())[0].samples
    calls = list(light_calls)
    def unexpected():
        pytest.fail("validation must not initialize gauges")
    original_init = dispatcher.init_room_gauges
    monkeypatch.setattr(dispatcher, "init_room_gauges", unexpected)
    rooms_file.write_text("rooms: {den: {plex_clients: [{title: x}]}}", encoding="utf-8")
    assert client.post("/rooms/validate").json() == {"status": "valid", "rooms": ["den"]}
    assert registry._state is before
    assert dispatcher._active_clients == active
    assert list(dispatcher.ROOM_ACTIVE_SESSIONS.collect())[0].samples == metrics
    assert light_calls == calls
    monkeypatch.setattr(dispatcher, "init_room_gauges", original_init)
    assert client.post("/rooms/reload").json() == {"status": "reloaded", "rooms": ["den"]}


@pytest.mark.parametrize("endpoint", ["validate", "reload"])
@pytest.mark.parametrize("source", [
    "rooms: [private-value", "rooms: {den: {plex_clients: [42]}}",
    "rooms: {den: {}, den: {}}",
    "rooms: {a: {plex_clients: [{uuid: private-value}]}, b: {plex_clients: [{uuid: private-value}]}}",
    "rooms: {a: {plex_clients: [{title: Private-Value}]}, b: {plex_clients: [{title: ' private-value '}]}}",
])
def test_invalid_room_endpoints_preserve_dispatch(client, registry, rooms_file, light_calls, monkeypatch, endpoint, source):
    from app import dispatcher

    post(client, payload("media.play", uuid="uuid-living"))
    drain()
    before = registry._state
    active = {key: dict(value) for key, value in dispatcher._active_clients.items()}
    metrics = list(dispatcher.ROOM_ACTIVE_SESSIONS.collect())[0].samples
    calls = list(light_calls)
    def unexpected():
        pytest.fail("failed candidates must not initialize gauges")
    monkeypatch.setattr(dispatcher, "init_room_gauges", unexpected)
    rooms_file.write_text(source, encoding="utf-8")
    resp = client.post(f"/rooms/{endpoint}")
    assert resp.status_code == 422
    assert all(set(error) == {"path", "code"} for error in resp.json()["detail"])
    assert "private-value" not in resp.text.lower()
    assert registry._state is before
    assert dispatcher._active_clients == active
    assert list(dispatcher.ROOM_ACTIVE_SESSIONS.collect())[0].samples == metrics
    assert light_calls == calls
    post(client, payload("media.stop", uuid="uuid-living"))
    drain()
    assert light_calls[-1] == ("restore", ["AA:BB"])


@pytest.mark.parametrize("endpoint", ["validate", "reload"])
@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_unavailable_room_endpoints(client, registry, monkeypatch, endpoint, failure):
    from pathlib import Path
    from app import dispatcher

    before = registry._state
    def fail(*args, **kwargs):
        raise failure("private-value")
    def unexpected():
        pytest.fail("unavailable config must not initialize gauges")
    monkeypatch.setattr(Path, "read_text", fail)
    monkeypatch.setattr(dispatcher, "init_room_gauges", unexpected)
    resp = client.post(f"/rooms/{endpoint}")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Rooms configuration unavailable"}
    assert registry._state is before


def test_invalid_startup_still_captures_webhook(client, rooms_file, monkeypatch, light_calls):
    from app import dispatcher
    import app.main as main
    from app.rooms import RoomRegistry

    rooms_file.write_text("rooms: [private-value", encoding="utf-8")
    startup = RoomRegistry(rooms_file)
    monkeypatch.setattr(main, "registry", startup)
    monkeypatch.setattr(dispatcher, "registry", startup)
    assert post(client, payload("media.play")).status_code == 200
    assert json.loads(client.log.read_text().splitlines()[0])["event"] == "media.play"
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    drain()
    assert light_calls == []


def test_counters_and_timestamp_seeded_from_sqlite_on_startup(tmp_path, monkeypatch, registry):
    import app.db
    import app.main as main

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

    # Re-import the app module against the seeded database, dropping the old collectors first.
    for collector in (main.EVENTS_TOTAL, main.LAST_EVENT_TIMESTAMP):
        REGISTRY.unregister(collector)
    monkeypatch.setattr(app.db, "DB_PATH", db_path)
    try:
        reloaded = importlib.reload(main)
        labels = {"player": "Living Room TV", "account": "known"}
        assert REGISTRY.get_sample_value("plex_webhook_events_total", {"event": "media.play", **labels}) == 3
        assert REGISTRY.get_sample_value("plex_webhook_events_total", {"event": "media.stop", **labels}) == 1
        other_player = {"event": "media.play", "player": "other", "account": "known"}
        assert REGISTRY.get_sample_value("plex_webhook_events_total", other_player) == 1
        other_all = {"event": "other", "player": "other", "account": "known"}
        assert REGISTRY.get_sample_value("plex_webhook_events_total", other_all) == 5000
        assert len(_event_series()) == 4
        assert REGISTRY.get_sample_value("plex_webhook_last_event_timestamp_seconds") == 1714564800.0
        reloaded.db_conn.close()
    finally:
        for collector in (main.EVENTS_TOTAL, main.LAST_EVENT_TIMESTAMP):
            REGISTRY.unregister(collector)
        monkeypatch.undo()
        importlib.reload(main).db_conn.close()


def _stored(client):
    return (client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            client.log.exists() and client.log.read_text() != "")


def _rejected(reason):
    return REGISTRY.get_sample_value("plex_webhook_rejected_requests_total", {"reason": reason}) or 0


def test_oversized_content_length_is_413_and_stores_nothing(client, light_calls):
    before = _rejected("body_too_large")
    resp = client.post("/webhook", data={"payload": "x" * (3 * 1024 * 1024)})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected("body_too_large") == before + 1
    drain()
    assert light_calls == []


def test_streamed_body_over_cap_without_content_length_is_413(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "MAX_BODY_BYTES", 1024)

    def chunks():
        yield b"payload=" + b"x" * 600
        yield b"y" * 600

    resp = client.post("/webhook", content=chunks(),
                       headers={"content-type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)


def test_too_many_fields_is_413_and_stores_nothing(client):
    before = _rejected("too_many_fields")
    data = {"payload": json.dumps(payload("media.play"))}
    data.update({f"extra{i}": "v" for i in range(20)})
    resp = client.post("/webhook", data=data, files={"thumb": ("t.jpg", b"x", "image/jpeg")})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected("too_many_fields") == before + 1


def test_too_many_files_is_413_and_stores_nothing(client):
    before = _rejected("too_many_files")
    files = [(f"f{i}", (f"t{i}.jpg", b"x", "image/jpeg")) for i in range(5)]
    resp = client.post("/webhook", data={"payload": json.dumps(payload("media.play"))}, files=files)
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected("too_many_files") == before + 1


def test_oversized_field_is_413_and_stores_nothing(client):
    before = _rejected("field_too_large")
    resp = client.post("/webhook", data={"payload": "x" * (600 * 1024)}, files={"t": ("t.jpg", b"x", "image/jpeg")})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected("field_too_large") == before + 1


def test_malformed_content_length_is_400(client):
    resp = client.post("/webhook", content=b"payload=x",
                       headers={"content-type": "application/x-www-form-urlencoded", "content-length": "abc"})
    assert resp.status_code == 400
    assert _stored(client) == (0, False)


def test_large_stored_payload_is_truncated_but_still_dispatched(client, light_calls, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "RAW_PAYLOAD_MAX_BYTES", 300)
    body = payload("media.play")
    body["Metadata"]["summary"] = "é" * 2000
    assert post(client, body).status_code == 200
    raw, truncated, title = client.conn.execute("SELECT raw_payload, raw_truncated, title FROM events").fetchone()
    assert truncated == 1 and len(raw.encode("utf-8")) <= 300 and title == "A Film"
    line = json.loads(client.log.read_text().splitlines()[0])
    assert line["payload_truncated"] is True and len(line["payload"].encode("utf-8")) <= 300
    drain()
    drain()
    assert light_calls == [("dim", ["AA:BB"])]


def test_normal_payload_is_not_marked_truncated(client):
    post(client, payload("media.play"))
    assert client.conn.execute("SELECT raw_truncated FROM events").fetchone() == (0,)
    assert "payload_truncated" not in json.loads(client.log.read_text().splitlines()[0])


def test_existing_database_gains_truncation_column(tmp_path, monkeypatch):
    import app.db as appdb

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, event TEXT, "
                     "account_title TEXT, player_title TEXT, player_uuid TEXT, media_type TEXT, title TEXT, "
                     "grandparent_title TEXT, rating_key TEXT, raw_payload TEXT NOT NULL)")
        conn.execute("INSERT INTO events (received_at, raw_payload) VALUES ('t', '{}')")
    monkeypatch.setattr(appdb, "DB_PATH", path)
    conn = appdb.get_connection()
    assert conn.execute("SELECT raw_truncated FROM events").fetchall() == [(0,)]
    conn.close()


def test_urlencoded_oversized_field_is_413_and_stores_nothing(client):
    before = _rejected("field_too_large")
    resp = client.post("/webhook", data={"payload": "x" * (600 * 1024)})
    assert resp.status_code == 413
    assert _stored(client) == (0, False)
    assert _rejected("field_too_large") == before + 1


def _hostile_post(client, raw):
    resp = client.post("/webhook", files={"payload": (None, raw)})
    assert resp.status_code == 200
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert len(client.log.read_text().splitlines()) == 1
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

def _webhook_metrics():
    return {(sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for metric in REGISTRY.collect() for sample in metric.samples
            if sample.name.startswith("plex_webhook_") and not sample.name.endswith("_created")}


def _assert_invalid_form(client, send):
    before = _webhook_metrics()
    resp = send()
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Invalid webhook form"}
    assert resp.headers["content-type"] == "application/json"
    assert _stored(client) == (0, False)
    assert [(r["source"], r["action"], r["outcome"], r["reason_code"], r["changed_fields"]) for r in rows(client)] == [
        ("webhook", "webhook.receipt", "rejected", "invalid_form", {})
    ]
    assert _webhook_metrics() == before


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
    before = _webhook_metrics()
    resp = client.post("/webhook", files={"payload": (None, json.dumps(body))})
    assert (resp.status_code, resp.json()) == (200, {"status": "received", "event": None})
    receipt = rows(client)
    assert [(r["outcome"], r["reason_code"]) for r in receipt] == [("rejected", "invalid_payload")]
    assert client.conn.execute(
        "SELECT event, account_title, player_title, player_uuid, media_type, title, grandparent_title, rating_key, "
        "raw_payload, raw_truncated FROM events").fetchall() == [
        (None, None, None, None, None, None, None, None, json.dumps(body), 0)
    ]
    line = json.loads(client.log.read_text(encoding="utf-8"))
    assert (line["event"], line["payload"]) == (None, json.loads(json.dumps(body)))
    after = _webhook_metrics()
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
    line = client.log.read_text(encoding="utf-8").splitlines()[-1]
    assert token not in line
    assert json.loads(line)["payload"] is None
    assert client.conn.execute("SELECT raw_payload FROM events").fetchone()[0] == "null"
    drain()
    drain()
    assert light_calls == []
