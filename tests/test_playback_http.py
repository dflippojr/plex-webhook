"""Playback journeys through HTTP, with only physical light I/O replaced."""
import json
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families

from app import db, dispatcher, lights
from conftest import drain, payload, post, rows

# Capture these before conftest.client installs its light recorder.
REAL_PREPARE = lights.prepare
REAL_READ_STATE = lights.read_state
REAL_APPLY_ACTION = lights.apply_action


class PhysicalLights:
    """Synthetic bulbs whose readings change only when a command is applied."""

    def __init__(self):
        self.state = {"AA:BB": 65, "plug1": 80}
        self.commands = []
        self.prepared = []

    def prepare(self, room_lights):
        self.prepared.append([light["id"] for light in room_lights])

    def read_state(self, light, timeout):
        transport = "lan" if light["brand"] == "govee" else "local"
        return lights.StateReading(True, True, self.state[light["id"]], transport)

    def apply(self, action, light, brightness=None, turn_on=True):
        self.commands.append((action, light["id"], brightness))
        self.state[light["id"]] = brightness
        transport = "lan" if light["brand"] == "govee" else "local"
        return lights.LightResult(
            light["brand"], light["id"], transport, "none",
            "command_sent" if transport == "lan" else "request_accepted_by_transport",
            "lan_command_sent" if transport == "lan" else "local_accepted",
        )


def metrics(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    }


def sample(values, name, **labels):
    return values.get((name, tuple(sorted(labels.items()))), 0)


class PlaybackJourney:
    def __init__(self, client, physical):
        self.client, self.physical = client, physical
        self.initial = physical.state.copy()
        self.before = metrics(client)
        self.payloads, self.expected_queued = [], []

    def send(self, event, title="Living Room TV", uuid="uuid-living", queued=None):
        body = payload(event, title, uuid, account="synthetic-viewer")
        response = post(self.client, body)
        # Complete each edge before the next one: coalescing must not hide work.
        drain()
        assert response.status_code == 200
        assert response.json() == {"status": "received", "event": event}
        self.payloads.append(body)
        self.expected_queued.append(queued)
        self.assert_capture()

    def assert_capture(self):
        stored = self.client.conn.execute(
            "SELECT id, received_at, event, raw_payload FROM events ORDER BY id"
        ).fetchall()
        captured = [json.loads(line) for line in self.client.log.read_text(encoding="utf-8").splitlines()]
        assert [json.loads(row[3]) for row in stored] == self.payloads
        assert captured == [
            {"received_at": row[1], "event": body["event"], "payload": body, "attachments": []}
            for row, body in zip(stored, self.payloads)
        ]
        assert [row[2] for row in stored] == [body["event"] for body in self.payloads]

        log = rows(self.client)
        receipts = [row for row in log if row["action"] == "webhook.receipt"]
        queued = [row for row in log if row["action"] == "light.action_queued"]
        assert len(receipts) == len(self.payloads)
        assert len({row["correlation_id"] for row in receipts}) == len(receipts)
        assert [(row["target_id"], row["detail"]["request"]) for row in queued] == [
            expected for expected in self.expected_queued if expected is not None
        ]
        assert [row["id"] for row in queued] == sorted(row["id"] for row in queued)
        for receipt, stored_row, expected in zip(receipts, stored, self.expected_queued):
            assert (receipt["outcome"], receipt["reason_code"]) == ("received", "accepted")
            assert int(receipt["target_id"]) == stored_row[0]
            linked = [row for row in queued if row["correlation_id"] == receipt["correlation_id"]]
            if expected is None:
                assert linked == []
            else:
                assert len(linked) == 1
                assert (linked[0]["target_id"], linked[0]["detail"]["request"]) == expected
                assert linked[0]["detail"]["event_id"] == stored_row[0]
                assert receipt["id"] < linked[0]["id"]

    def assert_room(self, room, light_id, *, active, brightness, dims, restores):
        current = metrics(self.client)
        assert current[("plex_dispatcher_room_active_sessions", (("room", room),))] == active
        for action, delta in (("dim", dims), ("restore", restores)):
            assert sample(current, "plex_dispatcher_actions_total", room=room, action=action) == (
                sample(self.before, "plex_dispatcher_actions_total", room=room, action=action) + delta
            )
        assert self.physical.state[light_id] == brightness
        record = db.get_restore_record(room, light_id)
        if active:
            assert record == {"was_off": False, "restore_percent": self.initial[light_id], "dim_percent": 20}
        else:
            assert record is None


@pytest.fixture
def journey(client, registry, monkeypatch):
    """The shared fixture owns/closes the local TestClient and event connection."""
    monkeypatch.setattr(lights, "prepare", REAL_PREPARE)
    monkeypatch.setattr(lights, "read_state", REAL_READ_STATE)
    monkeypatch.setattr(lights, "apply_action", REAL_APPLY_ACTION)
    physical = PhysicalLights()
    monkeypatch.setitem(lights._controllers, "govee", physical)
    monkeypatch.setitem(lights._controllers, "tuya", physical)
    # Restore storage, raw capture and audit use the same disposable SQLite file.
    path = Path(client.conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(db, "DB_PATH", path)
    dispatcher.init_room_gauges()
    world = PlaybackJourney(client, physical)
    try:
        yield world
    finally:
        drain()  # Finish worker writes before fixture-owned connections/patches close.


def test_playback_lifecycle_is_idempotent_and_observable(journey):
    for event, queued, active, brightness, dims, restores in [
        ("media.play", "dim", 1, 20, 1, 0),
        ("media.play", None, 1, 20, 1, 0),
        ("media.pause", "restore", 0, 65, 1, 1),
        ("media.stop", None, 0, 65, 1, 1),
        ("media.stop", None, 0, 65, 1, 1),
        ("media.resume", "dim", 1, 20, 2, 1),
        ("media.stop", "restore", 0, 65, 2, 2),
    ]:
        journey.send(event, queued=("living_room", queued) if queued else None)
        journey.assert_room("living_room", "AA:BB", active=active, brightness=brightness, dims=dims, restores=restores)
    assert journey.physical.commands == [
        ("dim", "AA:BB", 20), ("restore", "AA:BB", 65),
        ("dim", "AA:BB", 20), ("restore", "AA:BB", 65),
    ]
    assert journey.physical.prepared  # Real prepare routing reached the controller.
    assert journey.client.conn.execute("SELECT * FROM light_restore_records").fetchall() == []


def test_two_rooms_keep_independent_playback_and_restore(journey):
    journey.physical.state["AA:BB"] = journey.initial["AA:BB"] = 60
    for event, title, uuid, queued, living, bedroom in [
        ("media.play", "Living Room TV", "uuid-living", ("living_room", "dim"), (1, 20, 1, 0), (0, 80, 0, 0)),
        ("media.play", "Bedroom Apple TV", "uuid-bed-tv", ("bedroom", "dim"), (1, 20, 1, 0), (1, 20, 1, 0)),
        ("media.stop", "Living Room TV", "uuid-living", ("living_room", "restore"), (0, 60, 1, 1), (1, 20, 1, 0)),
        ("media.stop", "Bedroom Apple TV", "uuid-bed-tv", ("bedroom", "restore"), (0, 60, 1, 1), (0, 80, 1, 1)),
    ]:
        journey.send(event, title, uuid, queued)
        for room, light_id, expected in (("living_room", "AA:BB", living), ("bedroom", "plug1", bedroom)):
            active, brightness, dims, restores = expected
            journey.assert_room(room, light_id, active=active, brightness=brightness, dims=dims, restores=restores)
    assert journey.physical.commands == [
        ("dim", "AA:BB", 20), ("dim", "plug1", 20),
        ("restore", "AA:BB", 60), ("restore", "plug1", 80),
    ]


def test_unmapped_and_unknown_client_stops_leave_lights_unchanged(journey):
    for event, title, uuid in [
        ("media.play", "Kitchen iPad", "synthetic-kitchen"),
        ("media.stop", "Kitchen iPad", "synthetic-kitchen"),
        ("media.pause", "Living Room TV", "uuid-living"),
        ("media.stop", "Living Room TV", "uuid-living"),
    ]:
        journey.send(event, title, uuid)
        journey.assert_room("living_room", "AA:BB", active=0, brightness=65, dims=0, restores=0)
        assert journey.physical.commands == []

    journey.send("media.play", uuid=None, queued=("living_room", "dim"))
    journey.assert_room("living_room", "AA:BB", active=1, brightness=20, dims=1, restores=0)
    journey.send("media.stop", uuid="synthetic-never-started")
    journey.assert_room("living_room", "AA:BB", active=1, brightness=20, dims=1, restores=0)
    assert journey.physical.commands == [("dim", "AA:BB", 20)]
    journey.send("media.stop", uuid=None, queued=("living_room", "restore"))
    journey.assert_room("living_room", "AA:BB", active=0, brightness=65, dims=1, restores=1)
    journey.assert_room("bedroom", "plug1", active=0, brightness=80, dims=0, restores=0)
    assert journey.physical.commands == [("dim", "AA:BB", 20), ("restore", "AA:BB", 65)]
