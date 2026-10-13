"""plex_dispatcher_light_results_total: one count per per-light result, fake controllers only."""
import sqlite3
from collections import Counter

import pytest
from prometheus_client import REGISTRY

from app import audit, dispatcher, lights
from conftest import records

ROOM = "den"
METRIC = "plex_dispatcher_light_results_total"
SETTINGS = {"dim": 30, "restore": 80}
ROOM_LIGHTS = [{"brand": "govee", "id": "g1"}, {"brand": "tuya", "id": "t1"}, {"brand": "tuya", "id": "t2"}]


class FakeController:
    """Every light reads as on at 60%; each apply returns the configured transport and outcome."""

    def __init__(self, replies):
        self.replies, self.state = replies, {}

    def prepare(self, room_lights):
        pass

    def read_state(self, light, timeout):
        return self.state.get(light["id"], lights.StateReading(True, True, 60))

    def apply(self, action, light, brightness=None):
        transport, outcome, reason = self.replies[light["id"]]
        self.state[light["id"]] = lights.StateReading(True, True, brightness)
        return lights.LightResult(light["brand"], light["id"], transport, "none", outcome, reason)


@pytest.fixture
def fake(monkeypatch):
    controller = FakeController({
        "g1": ("lan", "command_sent", "lan_command_sent"),
        "t1": ("cloud", "request_accepted_by_transport", "cloud_accepted"),
        "t2": ("local", "failed", "unexpected_error"),
    })
    monkeypatch.setitem(lights._controllers, "govee", controller)
    monkeypatch.setitem(lights._controllers, "tuya", controller)
    return controller


@pytest.fixture
def audit_path(tmp_path):
    path = tmp_path / "audit.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(audit.SCHEMA)
    return path


def context_for(path):
    return dispatcher.AuditContext(lambda **fields: audit.append(path, **fields), audit.correlation_id())


def snapshot(room=ROOM):
    """Every series of the metric for ``room``, keyed by (brand, transport, outcome)."""
    return {(s.labels["brand"], s.labels["transport"], s.labels["outcome"]): s.value
            for metric in REGISTRY.collect() if metric.name == METRIC.removesuffix("_total")
            for s in metric.samples if s.name == METRIC and s.labels["room"] == room}


def delta(before, after):
    return {key: after[key] - before.get(key, 0) for key in after if after[key] != before.get(key, 0)}


def test_dim_counts_each_light_result_once_by_brand_transport_and_outcome(fake):
    before = snapshot()
    dispatcher._apply(ROOM, "dim", ROOM_LIGHTS, None, settings=SETTINGS)
    assert delta(before, snapshot()) == {
        ("govee", "lan", "command_sent"): 1,
        ("tuya", "cloud", "request_accepted_by_transport"): 1,
        ("tuya", "local", "failed"): 1,
    }


def test_restore_skipping_a_manually_changed_light_counts_none_skipped(fake):
    dispatcher._apply(ROOM, "dim", ROOM_LIGHTS[:1], None, settings=SETTINGS)
    fake.state["g1"] = lights.StateReading(True, True, 90)  # changed by hand after the dim
    before = snapshot()
    dispatcher._apply(ROOM, "restore", ROOM_LIGHTS[:1], None, settings=SETTINGS)
    assert delta(before, snapshot()) == {("govee", "none", "skipped"): 1}


def test_unknown_brands_are_counted_as_other_with_a_fixed_label_set(fake):
    room = "attic"
    unknown = [{"brand": f"brand{i}", "id": f"x{i}"} for i in range(25)] + [{"brand": "hue", "id": "h1"}]
    dispatcher._apply(room, "dim", unknown, None, settings=SETTINGS)
    counts = snapshot(room)
    assert {brand for brand, _, _ in counts} == {"other"}
    assert sum(counts.values()) == len(unknown)
    assert all(transport in audit.LIGHT_TRANSPORTS for _, transport, _ in counts)


def test_counts_match_the_light_result_audit_records(fake, audit_path):
    context = context_for(audit_path)
    before = snapshot()
    dispatcher._apply(ROOM, "dim", ROOM_LIGHTS, context, settings=SETTINGS)
    fake.state["g1"] = lights.StateReading(True, True, 90)
    dispatcher._apply(ROOM, "restore", ROOM_LIGHTS, context, settings=SETTINGS)
    audited = Counter((r["detail"]["brand"], r["detail"]["transport"], r["outcome"])
                      for r in records(audit_path) if r["action"] == "light.result")
    assert sum(audited.values()) == 6
    assert delta(before, snapshot()) == dict(audited)


def test_results_are_counted_without_an_audit_context_or_when_audit_writes_fail(fake):
    def failing(**fields):
        raise OSError("disk full")

    before = snapshot()
    dispatcher._apply(ROOM, "dim", ROOM_LIGHTS[:1], None, settings=SETTINGS)
    dispatcher._apply(ROOM, "restore", ROOM_LIGHTS[:1], dispatcher.AuditContext(failing, "c"), settings=SETTINGS)
    assert delta(before, snapshot()) == {("govee", "lan", "command_sent"): 2}


def test_a_backend_that_reports_nothing_counts_nothing(monkeypatch):
    monkeypatch.setattr(lights, "apply_action", lambda action, room_lights, brightness=None: None)
    before = snapshot()
    dispatcher._apply(ROOM, "on", ROOM_LIGHTS, None)
    assert delta(before, snapshot()) == {}
