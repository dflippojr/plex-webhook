"""GET /rooms/{room}/lights: a read-only admin check of a room's lights, with fake transports only."""
import json
import logging
import threading

import pytest
import yaml
from prometheus_client import REGISTRY

import app.main as main
from app import db, dispatcher, lights
from conftest import drain, rows

REAL_READ_STATE, REAL_PREPARE = lights.read_state, lights.prepare
ROOM = "den"
PATH = f"/rooms/{ROOM}/lights"
LIGHTS = [
    {"brand": "govee", "id": "g-lan", "name": "Lamp", "model": "H6159"},
    {"brand": "govee", "id": "g-cloud", "model": "H6159"},
    {"brand": "tuya", "id": "t-local", "name": "Plug"},
    {"brand": "tuya", "id": "t-cloud"},
    {"brand": "govee", "id": "g-dead", "name": "Dead", "model": "H6159"},
]
# What must never reach a response: addresses, keys and exception text from the transports.
LEAKS = ("10.0.0.9", "SYNTHETIC-LOCAL-KEY", "SYNTHETIC-API-KEY", "boom", "Traceback", "H6159")


def failed(light, timeout):
    raise OSError("boom at 10.0.0.9 with SYNTHETIC-LOCAL-KEY")


def answers(**by_id):
    """A transport step that answers for the given light ids and fails (with leaky text) for the rest."""
    def step(light, timeout):
        if light["id"] in by_id:
            return by_id[light["id"]]
        return failed(light, timeout)
    return step


@pytest.fixture
def den(client, rooms_file, registry, monkeypatch):
    """The client fixture, with a room of one light per transport and real read paths over fake steps."""
    rooms = {ROOM: {"plex_clients": [{"title": "Den TV"}], "lights": LIGHTS},
             "hall": {"plex_clients": [{"title": "Hall TV"}], "lights": [{"brand": "govee", "id": "h1"}]}}
    rooms_file.write_text(yaml.safe_dump({"rooms": rooms}), encoding="utf-8")
    registry.reload()
    monkeypatch.setattr(lights, "read_state", REAL_READ_STATE)
    monkeypatch.setattr(lights, "prepare", REAL_PREPARE)
    monkeypatch.setenv("GOVEE_API_KEY", "SYNTHETIC-API-KEY")
    govee, tuya = lights._controllers["govee"], lights._controllers["tuya"]
    monkeypatch.setattr(govee, "prepare", lambda room_lights, timeout=None: None)
    monkeypatch.setattr(govee, "_read_lan", answers(**{"g-lan": lights.StateReading(True, True, 60)}))
    monkeypatch.setattr(govee, "_read_cloud", answers(**{"g-cloud": lights.StateReading(True, False, 30)}))
    monkeypatch.setattr(tuya, "_read_local", answers(**{"t-local": lights.StateReading(True, True, 40)}))
    monkeypatch.setattr(tuya, "_read_cloud", answers(**{"t-cloud": lights.StateReading(True, False, None)}))

    def no_apply(*args, **kwargs):
        raise AssertionError("a light check must never command a light")

    for controller in (govee, tuya):
        monkeypatch.setattr(controller, "apply", no_apply)
    return client


def check_records(client):
    return [r for r in rows(client) if r["action"] == "lights.check"]


def read(entry):
    return entry["read"]


def test_reports_each_transport_in_config_order(den):
    response = den.get(PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["room"] == ROOM
    assert body["lights"] == [
        {"id": "g-lan", "brand": "govee", "name": "Lamp", "restore_record": False,
         "read": {"ok": True, "on": True, "brightness_percent": 60, "transport": "lan"}},
        {"id": "g-cloud", "brand": "govee", "name": None, "restore_record": False,
         "read": {"ok": True, "on": False, "brightness_percent": 30, "transport": "cloud"}},
        {"id": "t-local", "brand": "tuya", "name": "Plug", "restore_record": False,
         "read": {"ok": True, "on": True, "brightness_percent": 40, "transport": "local"}},
        {"id": "t-cloud", "brand": "tuya", "name": None, "restore_record": False,
         "read": {"ok": True, "on": False, "brightness_percent": None, "transport": "cloud"}},
        {"id": "g-dead", "brand": "govee", "name": "Dead", "restore_record": False,
         "read": {"ok": False, "on": None, "brightness_percent": None, "transport": "none"}},
    ]


def test_response_never_carries_addresses_keys_or_exception_text(den, monkeypatch):
    monkeypatch.setattr(lights, "_secrets_cache", {"t-local": {"ip": "10.0.0.9", "local_key": "SYNTHETIC-LOCAL-KEY"}})
    text = den.get(PATH).text
    assert not [leak for leak in LEAKS if leak in text]
    assert not [leak for leak in LEAKS if leak in json.dumps(check_records(den))]


def test_unknown_transport_or_values_from_a_controller_are_not_echoed(den, monkeypatch):
    weird = lights.StateReading(True, "10.0.0.9", "boom", "10.0.0.9")
    monkeypatch.setattr(lights._controllers["govee"], "read_state", lambda light, timeout: weird)
    entry = den.get(PATH).json()["lights"][0]
    assert read(entry) == {"ok": True, "on": None, "brightness_percent": None, "transport": "none"}


def test_check_is_read_only(den, light_calls):
    db.put_restore_record(ROOM, "g-lan", False, 70, 20)
    dispatcher._active_clients["hall"] = {"player": 9e18}
    before = (db.get_restore_record(ROOM, "g-lan"), db.rooms_with_restore_records(), json.dumps(dispatcher._active_clients))
    lights_ = den.get(PATH).json()["lights"]
    assert [entry["restore_record"] for entry in lights_] == [True, False, False, False, False]
    after = (db.get_restore_record(ROOM, "g-lan"), db.rooms_with_restore_records(), json.dumps(dispatcher._active_clients))
    assert after == before and light_calls == []


def test_success_writes_one_admin_record_without_light_values(den):
    den.get(PATH)
    [record] = check_records(den)
    assert (record["outcome"], record["reason_code"], record["target_kind"], record["target_id"]) == (
        "completed", "read", "room", ROOM)
    assert (record["actor_id"], record["actor_verified"]) == ("owner-admin", 1)
    assert record.get("detail") is None and record["changed_fields"] in ({}, "{}")
    assert not [light["id"] for light in LIGHTS if light["id"] in json.dumps(record)]


def test_unknown_room_is_404_and_audited_without_the_key(den):
    response = den.get("/rooms/attic-SENTINEL/lights")
    assert response.status_code == 404 and response.json() == {"detail": "Unknown room"}
    [record] = check_records(den)
    assert (record["outcome"], record["reason_code"], record["target_id"]) == ("rejected", "unknown_room", None)
    assert record["actor_id"] == "owner-admin" and "SENTINEL" not in json.dumps(record)


@pytest.mark.parametrize("headers,status,code", [
    ({}, 401, "missing_credential"),
    ({"Authorization": "Bearer wrong-token-wrong-token-wrong"}, 401, "invalid_credential"),
])
def test_denied_without_a_valid_token(den, headers, status, code):
    den.headers.pop("Authorization")
    counter = REGISTRY.get_sample_value("plex_webhook_admin_denials_total", {"reason": code})
    response = den.get(PATH, headers=headers)
    assert response.status_code == status and response.json() == {"detail": "Admin credential required"}
    assert REGISTRY.get_sample_value("plex_webhook_admin_denials_total", {"reason": code}) == (counter or 0) + 1
    [record] = check_records(den)
    assert (record["outcome"], record["reason_code"], record["actor_id"]) == ("denied", code, None)


@pytest.mark.parametrize("token", [None, "short"])
def test_unset_or_weak_token_is_503(den, monkeypatch, token):
    if token is None:
        monkeypatch.delenv("ADMIN_API_TOKEN")
    else:
        monkeypatch.setenv("ADMIN_API_TOKEN", token)
    counter = REGISTRY.get_sample_value("plex_webhook_admin_denials_total", {"reason": "auth_unconfigured"})
    response = den.get(PATH)
    assert response.status_code == 503 and response.json() == {"detail": "Admin authentication unavailable"}
    assert REGISTRY.get_sample_value("plex_webhook_admin_denials_total", {"reason": "auth_unconfigured"}) == (counter or 0) + 1
    [record] = check_records(den)
    assert (record["outcome"], record["reason_code"]) == ("denied", "auth_unconfigured")


# --- the light-action worker -----------------------------------------------------


@pytest.fixture
def blocked_worker():
    release, started = threading.Event(), threading.Event()

    def block():
        started.set()
        release.wait(timeout=10)

    dispatcher._action_executor.submit(block)
    assert started.wait(timeout=5)
    yield release
    release.set()
    drain()


def queued_keys():
    with dispatcher._action_executor._ready:
        return [entry[0] for entry in dispatcher._action_executor._entries]


def wait_for(condition):
    for _ in range(500):
        if condition():
            return
        threading.Event().wait(0.01)
    raise AssertionError("condition never held")


def test_check_runs_on_the_worker_after_an_already_queued_action(den, monkeypatch, blocked_worker):
    log = []
    real = lights._controllers["govee"].read_state

    def recording_read(light, timeout):
        log.append(("read", light["id"], threading.current_thread().name))
        return real(light, timeout)

    monkeypatch.setattr(lights._controllers["govee"], "read_state", recording_read)
    dispatcher._action_executor.submit(lambda: log.append(("queued action", threading.current_thread().name)),
                                       key=("room", ROOM))
    responses = []
    request = threading.Thread(target=lambda: responses.append(den.get(PATH)))
    request.start()
    wait_for(lambda: ("check", ROOM) in queued_keys())
    assert log == []  # nothing ran while the earlier action was still waiting
    blocked_worker.set()
    request.join(timeout=10)
    assert responses[0].status_code == 200
    assert log[0] == ("queued action", "light-action")
    assert [entry[:2] for entry in log[1:]] == [("read", "g-lan"), ("read", "g-cloud"), ("read", "g-dead")]
    assert {entry[2] for entry in log[1:]} == {"light-action"}


def test_blocked_read_times_out_with_504(den, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(main, "LIGHT_CHECK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(lights._controllers["govee"], "read_state",
                        lambda light, timeout: release.wait(10) and lights.StateReading.failed())
    try:
        response = den.get(PATH)
    finally:
        release.set()
        drain()
    assert response.status_code == 504 and response.json() == {"detail": "Light check timed out"}
    [record] = check_records(den)
    assert (record["outcome"], record["reason_code"], record["target_id"]) == ("failed", "timed_out", ROOM)


def test_timed_out_check_that_never_started_is_skipped(den, monkeypatch, blocked_worker):
    reads = []
    monkeypatch.setattr(main, "LIGHT_CHECK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(lights._controllers["govee"], "read_state", lambda light, timeout: reads.append(light))
    assert den.get(PATH).status_code == 504
    blocked_worker.set()
    drain()
    assert reads == []


def test_full_queue_drops_the_check_not_a_queued_action(den, monkeypatch, blocked_worker):
    monkeypatch.setattr(dispatcher._action_executor, "limit", 1)
    ran = []
    queued = dispatcher._action_executor.submit(ran.append, "dim", key=("room", "hall"))
    response = den.get(PATH)
    assert response.status_code == 503 and response.json() == {"detail": "Light check queue full"}
    assert queued_keys() == [("room", "hall")] and not queued.cancelled()
    monkeypatch.setattr(dispatcher._action_executor, "limit", dispatcher.ACTION_QUEUE_LIMIT)
    blocked_worker.set()
    queued.result(timeout=5)
    assert ran == ["dim"]
    [record] = check_records(den)
    assert (record["outcome"], record["reason_code"]) == ("failed", "queue_overflow")


def dropped_actions(reason):
    return REGISTRY.get_sample_value("plex_dispatcher_actions_dropped_total", {"room": ROOM, "reason": reason}) or 0


def test_refused_check_is_not_counted_or_logged_as_a_dropped_action(den, monkeypatch, blocked_worker, caplog):
    monkeypatch.setattr(dispatcher._action_executor, "limit", 1)
    before = dropped_actions("queue_overflow")
    queued = dispatcher._action_executor.submit(lambda: None, key=("room", "hall"))
    with caplog.at_level(logging.WARNING, logger="plex-webhook"):
        future = dispatcher.check_lights(ROOM)
    with pytest.raises(dispatcher.CheckDropped):
        future.result(timeout=1)
    monkeypatch.setattr(dispatcher._action_executor, "limit", dispatcher.ACTION_QUEUE_LIMIT)
    assert not queued.cancelled()
    assert dropped_actions("queue_overflow") == before
    assert not any("dispatch_queue_overflow" in record.getMessage() for record in caplog.records)


def test_superseded_check_is_not_counted_as_a_dropped_action(den, blocked_worker):
    before = dropped_actions("superseded")
    first, second = dispatcher.check_lights(ROOM), dispatcher.check_lights(ROOM)
    with pytest.raises(dispatcher.CheckDropped):
        first.result(timeout=1)
    assert dropped_actions("superseded") == before
    blocked_worker.set()
    assert len(second.result(timeout=10)) == len(LIGHTS)


def test_check_error_is_500_audited_and_redacted(den, monkeypatch, caplog):
    def broken(room_key, room_lights):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(dispatcher, "_check", broken)
    with caplog.at_level(logging.DEBUG, logger="plex-webhook"):
        response = den.get(PATH)
    assert response.status_code == 500 and response.json() == {"detail": "Light check failed"}
    assert "SECRET" not in response.text
    [record] = check_records(den)
    assert (record["outcome"], record["reason_code"], record["target_id"]) == ("failed", "check_error", ROOM)
    assert any("reason=light_check_failed" in r.getMessage() for r in caplog.records)
    assert "SECRET" not in caplog.text


def test_cancelled_check_superseded_by_a_newer_check_stays_cancelled(den, blocked_worker, caplog):
    with caplog.at_level(logging.ERROR, logger="plex-webhook"):
        first = dispatcher.check_lights(ROOM)
        assert first.cancel()
        second = dispatcher.check_lights(ROOM)  # same key, so it supersedes the cancelled check
    assert first.cancelled()
    assert not any("dispatcher_action_failed" in r.getMessage() for r in caplog.records)
    blocked_worker.set()
    assert len(second.result(timeout=10)) == len(LIGHTS)
