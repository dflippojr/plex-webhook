"""Webhook-to-hub journeys for a ``hass`` room, with only the HTTP boundary (requests.get/post) faked."""
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest
import requests

from app import lights
from conftest import drain, payload, post

# Capture the real backend before the client fixture installs its light recorders.
_prepare, _read_state, _apply_action = lights.prepare, lights.read_state, lights.apply_action

HUB = "http://synthetic-hub.invalid:8123"
# Distinctive, so any copy of it anywhere is unmistakable.
TOKEN = "SYNTHETIC-HASS-TOKEN-7f3a9c1e5b2d4086"
ENTITIES = ("light.synthetic_lamp", "light.synthetic_strip")
ROOMS = """
rooms:
  cinema:
    dim_brightness_percent: 20
    restore_brightness_percent: 100
    plex_clients:
      - {title: Synthetic TV, uuid: synthetic-player}
    lights:
      - {brand: hass, id: light.synthetic_lamp, name: Lamp}
      - {brand: hass, id: light.synthetic_strip}
"""


class Hub:
    """A fake Home Assistant: light states by entity id, changed by light/turn_on calls."""

    def __init__(self):
        self.state = {entity: {"state": "on", "brightness": 179} for entity in ENTITIES}  # 179/255 is 70%
        self.commands, self.reads, self.fail = [], [], None

    def get(self, url, *, headers, timeout, allow_redirects):
        assert headers == {"Authorization": f"Bearer {TOKEN}"} and allow_redirects is False
        prefix = f"{HUB}/api/states/"
        assert url.startswith(prefix)
        entity = url[len(prefix):]
        self.reads.append(entity)
        if self.fail == "exception":
            raise requests.ConnectionError(f"GET {url} headers={headers!r} failed")
        if self.fail == "status":
            return SimpleNamespace(status_code=401, json=lambda: {"message": f"bad token {TOKEN}"})
        light = self.state[entity]
        body = {"entity_id": entity, "state": light["state"], "attributes": {"brightness": light["brightness"]}}
        return SimpleNamespace(status_code=200, json=lambda: body)

    def post(self, url, *, headers, json, timeout, allow_redirects):
        assert url == f"{HUB}/api/services/light/turn_on"
        assert headers == {"Authorization": f"Bearer {TOKEN}"} and allow_redirects is False
        self.commands.append((json["entity_id"], json["brightness_pct"]))
        if self.fail == "exception":
            raise requests.ConnectionError(f"POST {url} headers={headers!r} body={json!r} failed")
        if self.fail == "status":
            return SimpleNamespace(status_code=500, text=f"error for {TOKEN}")
        self.state[json["entity_id"]] = {"state": "on", "brightness": round(json["brightness_pct"] * 255 / 100)}
        return SimpleNamespace(status_code=200, text="[]")


@pytest.fixture
def hub(client, rooms_file, registry, monkeypatch):
    rooms_file.write_text(ROOMS, encoding="utf-8")
    registry.reload()
    monkeypatch.setenv("HASS_URL", HUB)
    monkeypatch.setenv("HASS_TOKEN", TOKEN)
    # Explicitly undo all three light backend recorders from the client fixture.
    monkeypatch.setattr(lights, "prepare", _prepare)
    monkeypatch.setattr(lights, "read_state", _read_state)
    monkeypatch.setattr(lights, "apply_action", _apply_action)
    fake = Hub()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    yield fake
    drain()  # settle queued work before monkeypatch restores the boundaries


def light_results(client, correlation):
    return client.conn.execute(
        "SELECT target_id, outcome, reason_code, detail FROM audit_events "
        "WHERE action = 'light.result' AND correlation_id = ? ORDER BY id", (correlation,)).fetchall()


def test_play_dims_and_stop_restores_a_hass_room(client, hub, restore_records):
    response = post(client, payload("media.play", "Synthetic TV", "synthetic-player"))
    assert response.status_code == 200
    drain()
    assert hub.commands == [(entity, 20) for entity in ENTITIES]
    assert [restore_records.get_restore_record("cinema", entity) for entity in ENTITIES] == [
        {"was_off": False, "restore_percent": 70, "dim_percent": 20}] * 2

    response = post(client, payload("media.stop", "Synthetic TV", "synthetic-player"))
    assert response.status_code == 200
    drain()
    assert hub.commands == [(entity, 20) for entity in ENTITIES] + [(entity, 70) for entity in ENTITIES]
    assert all(light == {"state": "on", "brightness": 178} for light in hub.state.values())
    assert restore_records.rooms_with_restore_records() == set()

    receipts = client.conn.execute(
        "SELECT correlation_id, target_id FROM audit_events WHERE action = 'webhook.receipt' ORDER BY id").fetchall()
    assert len(receipts) == 2
    for (correlation, event_id), action in zip(receipts, ("dim", "restore")):
        results = light_results(client, correlation)
        assert [row[:3] for row in results] == [
            (entity, "request_accepted_by_transport", "hub_accepted") for entity in ENTITIES]
        for *_, raw in results:
            detail = json.loads(raw)
            assert (detail["brand"], detail["transport"], detail["credential_source"], detail["request"]) == (
                "hass", "hub", "environment", action)
            assert str(detail["event_id"]) == event_id

    series = client.service.metrics.registry.get_sample_value(
        "plex_dispatcher_light_results_total",
        {"room": "cinema", "brand": "hass", "transport": "hub", "outcome": "request_accepted_by_transport"})
    assert series == 4


def test_off_hass_light_is_left_off(client, hub):
    hub.state[ENTITIES[1]] = {"state": "off", "brightness": None}
    post(client, payload("media.play", "Synthetic TV", "synthetic-player"))
    drain()
    assert hub.commands == [(ENTITIES[0], 20)]


def test_unreadable_hass_light_is_not_switched_on_by_a_dim(client, hub):
    hub.fail = "status"
    post(client, payload("media.play", "Synthetic TV", "synthetic-player"))
    drain()
    assert hub.commands == []
    [(correlation,)] = client.conn.execute(
        "SELECT correlation_id FROM audit_events WHERE action = 'webhook.receipt'").fetchall()
    assert [row[1:3] for row in light_results(client, correlation)] == [
        ("skipped", "brightness_only_unsupported")] * 2


def test_hub_token_never_leaves_the_authorization_header(client, hub, caplog):
    caplog.set_level(logging.DEBUG)
    responses = []

    def play_and_stop():
        for event in ("media.play", "media.stop"):
            responses.append(post(client, payload(event, "Synthetic TV", "synthetic-player")))
            drain()
        responses.append(client.get("/rooms/cinema/lights"))

    play_and_stop()                       # reads and accepted commands
    hub.fail = "status"                   # non-2xx replies whose bodies carry the token
    play_and_stop()
    hub.fail = "exception"                # exceptions whose messages carry the URL, headers and token
    play_and_stop()
    responses.append(client.get("/metrics"))
    responses.append(client.get("/rooms"))

    assert len(hub.commands) >= 4 and len(hub.reads) >= 6
    assert {row[0] for row in client.conn.execute(
        "SELECT reason_code FROM audit_events WHERE action = 'light.result'")} >= {
        "hub_accepted", "hub_rejected", "request_failed", "brightness_only_unsupported"}
    assert all(response.status_code == 200 for response in responses)

    for record in caplog.records:
        rendered = " ".join(str(part) for part in (record.getMessage(), record.msg, record.args, record.exc_text,
                                                   record.stack_info))
        assert TOKEN not in rendered
        assert record.exc_info is None or TOKEN not in repr(record.exc_info[1])
    assert TOKEN not in caplog.text

    for response in responses:
        assert TOKEN not in response.text
        assert TOKEN not in json.dumps(dict(response.headers))
        assert HUB not in response.text

    db_path = client.conn.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(db_path) as conn:
        dump = "\n".join(conn.iterdump())
    assert TOKEN not in dump
    assert HUB not in dump
