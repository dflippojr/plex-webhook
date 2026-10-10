import copy
import logging
import os
import threading
from collections import Counter as OutcomeCounter, deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable

from prometheus_client import Counter, Gauge

from app import audit, db, lights
from app.rooms import DEFAULT_DIM_PERCENT, DEFAULT_RESTORE_PERCENT, registry

logger = logging.getLogger("plex-webhook")

ACTIVATE_EVENTS = {"media.play", "media.resume"}
DEACTIVATE_EVENTS = {"media.pause", "media.stop"}

ROOM_ACTIVE_SESSIONS = Gauge(
    "plex_dispatcher_room_active_sessions",
    "Number of Plex clients currently playing in a room",
    ["room"],
)
DISPATCH_ACTIONS_TOTAL = Counter(
    "plex_dispatcher_actions_total",
    "Total light actions dispatched",
    ["room", "action"],
)

LIGHT_DECISIONS_TOTAL = Counter(
    "plex_dispatcher_light_decisions_total",
    "Per-light brightness decisions taken when dimming and restoring",
    ["room", "decision"],
)

DISPATCH_DROPPED_TOTAL = Counter(
    "plex_dispatcher_actions_dropped_total",
    "Queued light actions dropped before they started",
    ["room", "reason"],
)

# A light further than this many percentage points from the dim level was changed by hand.
MANUAL_CHANGE_TOLERANCE_PERCENT = 5



def _env_int(name, default):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


class ActionQueue:
    """One worker that applies queued actions in order, coalescing them by key.

    Submitting with a key drops any not-yet-started entry with the same key, and the
    new entry goes to the back, so the latest state for a room wins and still runs after
    every earlier action for that room. When the queue holds ``limit`` entries the oldest
    queued one is dropped. A dropped entry's Future is cancelled and its ``on_drop``
    callback is called with ``"superseded"`` or ``"queue_overflow"`` on the submitting
    thread, outside the lock.
    """

    def __init__(self, limit, name="light-action"):
        self.limit = limit
        self._name = name
        self._entries = deque()
        self._ready = threading.Condition()
        self._worker = None

    def submit(self, fn, *args, key=None, on_drop=None, **kwargs):
        future = Future()
        dropped = []
        with self._ready:
            if key is not None:
                for entry in [entry for entry in self._entries if entry[0] == key]:
                    self._entries.remove(entry)
                    dropped.append((entry, "superseded"))
            while len(self._entries) >= self.limit:
                dropped.append((self._entries.popleft(), "queue_overflow"))
            self._entries.append((key, future, fn, args, kwargs, on_drop))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._worker.start()
            self._ready.notify()
        for (_, dropped_future, _, _, _, callback), reason in dropped:
            dropped_future.cancel()
            if callback is not None:
                try:
                    callback(reason)
                except Exception:
                    logger.error("reason=dispatcher_action_failed operation=drop")
        return future

    def pending(self):
        with self._ready:
            return len(self._entries)

    def _run(self):
        while True:
            with self._ready:
                while not self._entries:
                    self._ready.wait()
                _, future, fn, args, kwargs, _ = self._entries.popleft()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)


# One worker applies every light action in the order it was queued, so a restore
# can never overtake an earlier dim for the same room, and a slow or unreachable
# light only delays later actions, never the event loop. A room's queued action is
# replaced by a newer one for the same room, and the queue is bounded.
ACTION_QUEUE_LIMIT = _env_int("DISPATCH_QUEUE_LIMIT", 32)
_action_executor = ActionQueue(ACTION_QUEUE_LIMIT)

# room_key -> set of client identifiers currently playing in that room
_active_clients: dict[str, set] = {}

# Rooms that may still hold restore records: dimmed and not yet restored, including
# before a restart, when the in-memory player state is gone but the records are not.
_pending_rooms: set[str] = set()


def _load_pending_rooms():
    try:
        _pending_rooms.update(db.rooms_with_restore_records())
        _pending_rooms.update(db.automation_sources())
    except Exception:
        logger.warning("reason=restore_records_unavailable operation=load_pending")


_load_pending_rooms()


@dataclass(frozen=True)
class AuditContext:
    """Receipt correlation and initiating actor carried across the worker boundary.

    ``record`` appends one audit record, owns its connection and never raises;
    a failure must not cause a light command to be retried or skipped.
    """

    record: Callable
    correlation: str
    event_id: int | None = None
    actor: str = "plex_server"


def init_room_gauges():
    """Export every configured room so dashboards see 0 after a restart, not a stale pre-restart value."""
    for room_key in registry.rooms:
        ROOM_ACTIVE_SESSIONS.labels(room=room_key).set(len(_active_clients.get(room_key, ())))


init_room_gauges()


def _client_id(payload: dict) -> str:
    player = (payload or {}).get("Player") or {}
    return player.get("uuid") or player.get("title") or "unknown"


def handle_event(payload: dict, audit_context: AuditContext | None = None):
    if not payload:
        return

    event = payload.get("event")
    if event not in ACTIVATE_EVENTS and event not in DEACTIVATE_EVENTS:
        return

    room_key = registry.resolve_room(payload)
    if room_key is None:
        logger.debug("event=%s from unmapped client, ignoring", event)
        return

    client_id = _client_id(payload)
    active = _active_clients.setdefault(room_key, set())
    was_active = len(active) > 0

    if event in ACTIVATE_EVENTS:
        active.add(client_id)
    else:
        active.discard(client_id)

    is_active = len(active) > 0
    ROOM_ACTIVE_SESSIONS.labels(room=room_key).set(len(active))

    if not was_active and is_active:
        _pending_rooms.add(room_key)
        _dispatch_lifecycle(room_key, "dim", audit_context)
    elif (was_active and not is_active) or (not was_active and event in DEACTIVATE_EVENTS and room_key in _pending_rooms):
        # The second case: a restart lost the player state but the dim's records survived.
        _pending_rooms.discard(room_key)
        _dispatch_lifecycle(room_key, "restore", audit_context)


def _dispatch_lifecycle(room_key, action, context):
    _dispatch(room_key, action, context)
    entries = copy.deepcopy(registry.automations_for(room_key, action))
    # Cleanup includes persisted targets, even if a reload removed their automation.
    snapshots = {room: (copy.deepcopy(registry.lights_for(room)), dict(registry.settings_for(room)))
                 for room in registry.rooms}
    blocked = {room for room in snapshots if _active_clients.get(room)}
    # Nothing is audited for automations until they run, so a dropped batch leaves no queued record.
    _action_executor.submit(_run_automations, room_key, action, entries, snapshots, blocked, context,
                            key=("automations", room_key), on_drop=lambda reason: _dropped(room_key, reason))


def _run_automations(source, event, entries, snapshots, blocked, context):
    """Run on the same worker as playback actions; automation actions never emit triggers."""
    try:
        if event == "restore":
            for target in sorted(db.automation_targets(source)):
                _automation_action(source, target, "restore", snapshots, blocked, context)
        for entry in entries:
            _automation_action(source, entry["target_room"], entry["action"], snapshots, blocked, context)
    except Exception:
        logger.error("reason=dispatcher_action_failed operation=automation")


def _automation_action(source, target, action, snapshots, blocked, context):
    if target not in snapshots:
        return
    room_lights, settings = snapshots[target]
    settings = dict(settings)
    if type(action) is int:
        settings["dim"] = action
        action = "dim"
    action_id = audit.correlation_id()
    if target in blocked:
        _record(context, "light.action_summary", "skipped", "own_playback_active",
                target, action, action_id, counts={})
        return
    _record(context, "light.action_queued", "queued", "accepted", target, action, action_id,
            targets=[{"brand": _text(light.get("brand")), "id": _text(light.get("id")),
                      "brightness": settings["dim"] if action == "dim" else None} for light in room_lights])
    _apply(target, action, room_lights, context, action_id, settings, automation_source=source)


def _dispatch(room_key: str, action: str, audit_context: AuditContext | None = None):
    """Snapshot the targets, queue the light action on the worker thread and return its Future.

    Targets are resolved now so a config reload after enqueue cannot change a queued action.
    """
    action_id = audit.correlation_id()

    def on_drop(reason):
        # The terminal record for a queued action that never ran; no light was touched.
        _dropped(room_key, reason)
        _record(audit_context, "light.action_summary", "skipped", reason, room_key, action, action_id, counts={})

    try:
        room_lights = copy.deepcopy(registry.lights_for(room_key))
        settings = dict(registry.settings_for(room_key))
    except Exception:
        # Reported by the worker through the same failure path as any other action error.
        return _action_executor.submit(_apply, room_key, action, None, audit_context, action_id,
                                       key=("room", room_key), on_drop=on_drop)
    level = settings["dim"] if action == "dim" else None  # a restore level is read per light at execution time
    _record(audit_context, "light.action_queued", "queued", "accepted", room_key, action, action_id,
            targets=[{"brand": _text(light.get("brand")), "id": _text(light.get("id")),
                      "brightness": level} for light in room_lights])
    return _action_executor.submit(_apply, room_key, action, room_lights, audit_context, action_id, settings,
                                   key=("room", room_key), on_drop=on_drop)


def _dropped(room_key, reason):
    DISPATCH_DROPPED_TOTAL.labels(room=room_key, reason=reason).inc()
    if reason == "queue_overflow":
        logger.warning("reason=dispatch_queue_overflow limit=%d", ACTION_QUEUE_LIMIT)


def _text(value):
    return value if isinstance(value, str) and value else "unknown"


def _record(context, name, outcome, reason, target, action, action_id, **detail):
    if context is None or action not in audit.LIGHT_REQUESTS:
        return
    detail.update(action_id=action_id, request=action, on_behalf_of=context.actor, event_id=context.event_id)
    try:
        context.record(action=name, outcome=outcome, reason_code=reason, correlation=context.correlation,
                       target_id=target if isinstance(target, str) and target else None, detail=detail)
    except Exception:
        logger.warning("reason=audit_write_failed")


def _summarize(results):
    """Room-level outcome from per-light evidence; sent/accepted is never physical verification."""
    if not results:
        return "skipped", "no_lights"
    counts = OutcomeCounter(result.outcome for result in results)
    sent = counts["command_sent"] + counts["request_accepted_by_transport"]
    bad = counts["failed"] + counts["skipped"]
    if bad == len(results):
        return ("skipped", "all_skipped") if counts["skipped"] == len(results) else ("failed", "all_failed")
    if bad:
        return "partial", "mixed_results"
    if counts["unconfirmed"]:
        return "unconfirmed", "all_unconfirmed" if not sent else "some_unconfirmed"
    return "completed_unverified", "all_sent"


def _report(context, room_key, action, action_id, results):
    for result in results or ():
        _record(context, "light.result", result.outcome, result.reason, result.target_id, action, action_id,
                brand=_text(result.brand), transport=result.transport,
                credential_source=result.credential_source, progress=result.progress,
                attempts=[{"transport": a.transport, "credential_source": a.credential_source,
                           "outcome": a.outcome, "reason_code": a.reason, "progress": a.progress}
                          for a in result.attempts])
    outcome, reason = _summarize(results) if results is not None else ("unconfirmed", "no_results")
    counts = dict(OutcomeCounter(result.outcome for result in results or ()))
    _record(context, "light.action_summary", outcome, reason, room_key, action, action_id, counts=counts)


def _decide(context, room_key, light, request, action_id, decision, reason, settings, restore=None, observed=None):
    """One per-light brightness decision: counted always, audited when the action is."""
    LIGHT_DECISIONS_TOTAL.labels(room=room_key, decision=decision).inc()
    _record(context, "light.decision", decision, reason, light.get("id"), request, action_id,
            brand=_text(light.get("brand")), dim_percent=settings["dim"], restore_percent=restore,
            observed_percent=observed)


def _skipped(light, reason):
    return lights.skipped_result(light, reason)


def _dim(room_key, room_lights, settings, context, action_id):
    """Record each light's own brightness, then dim the ones that are on. Never turns a light on."""
    dim, results, targets = settings["dim"], [], []
    for light in room_lights:
        light_id = _text(light.get("id"))
        existing = db.get_restore_record(room_key, light_id)
        if existing:  # a re-dim before the pending restore ran keeps the original value
            if existing["was_off"]:
                _decide(context, room_key, light, "dim", action_id, "dim_skipped_off", "was_off", settings)
                results.append(_skipped(light, "light_off"))
                continue
            _decide(context, room_key, light, "dim", action_id, "dim_kept_original", "record_exists", settings,
                    existing["restore_percent"])
            # A newer automation or the target's own playback changes the expected level,
            # while preserving the brightness from before the first dim.
            db.put_restore_record(room_key, light_id, False, existing["restore_percent"], dim)
            targets.append(light)
            continue
        reading = lights.read_state(light)
        if reading.ok and not reading.on:
            db.put_restore_record(room_key, light_id, True, None, dim)
            _decide(context, room_key, light, "dim", action_id, "dim_skipped_off", "was_off", settings)
            results.append(_skipped(light, "light_off"))
            continue
        restore, reason = (reading.brightness, "read_ok") if reading.ok else (settings["restore"], "read_failed")
        if restore == dim:  # a stored value equal to the dim level could never undo the dim
            restore, reason = settings["restore"], "at_dim_level"
        if restore != dim:
            db.put_restore_record(room_key, light_id, False, restore, dim)
        _decide(context, room_key, light, "dim", action_id, "dim_recorded", reason, settings, restore,
                reading.brightness)
        targets.append(light)
    applied = lights.apply_action("dim", targets, dim) if targets else []
    return results + applied if isinstance(applied, list) else None


def _restore(room_key, room_lights, settings, context, action_id):
    """Put each recorded light back unless it was off, or changed by hand while dimmed."""
    results, targets, levels = [], [], {}
    for light in room_lights:
        light_id = _text(light.get("id"))
        record = db.get_restore_record(room_key, light_id)
        if record is None:
            results.append(_skipped(light, "no_record"))
            continue
        dim, restore = record["dim_percent"], record["restore_percent"]
        if record["was_off"]:
            decision, reason, observed = "skipped_was_off", "was_off", None
        else:
            reading = lights.read_state(light)
            observed = reading.brightness
            if not reading.ok:
                decision, reason = "restore_without_read", "read_failed"
            elif not reading.on:
                decision, reason = "skipped_manual_change", "turned_off"
            elif abs(reading.brightness - dim) > MANUAL_CHANGE_TOLERANCE_PERCENT:
                decision, reason = "skipped_manual_change", "brightness_changed"
            else:
                decision, reason = "restored", "within_tolerance"
        db.delete_restore_record(room_key, light_id)
        _decide(context, room_key, light, "restore", action_id, decision, reason, {**settings, "dim": dim},
                restore, observed)
        if decision in ("restored", "restore_without_read"):
            targets.append(light)
            levels[light_id] = restore
        else:
            results.append(_skipped(light, "light_off" if record["was_off"] else "manual_change"))
    applied = lights.apply_action("restore", targets, levels) if targets else []
    return results + applied if isinstance(applied, list) else None


def _apply(room_key: str, action: str, room_lights=None, audit_context=None, action_id=None, settings=None,
           automation_source=None):
    action_id = action_id or audit.correlation_id()
    settings = settings or {"dim": DEFAULT_DIM_PERCENT, "restore": DEFAULT_RESTORE_PERCENT}
    try:
        # Also check at execution: a queued automation must yield to playback that
        # started while an earlier light command was still running.
        if automation_source is not None and _active_clients.get(room_key):
            _record(audit_context, "light.action_summary", "skipped", "own_playback_active",
                    room_key, action, action_id, counts={})
            return
        if action in ("dim", "restore"):
            db.set_automation_owner(room_key, automation_source if action == "dim" else None)
        if room_lights is None:
            room_lights = registry.lights_for(room_key)
        if action == "dim":
            results = _dim(room_key, room_lights, settings, audit_context, action_id)
        elif action == "restore":
            results = _restore(room_key, room_lights, settings, audit_context, action_id)
        else:
            results = lights.apply_action(action, room_lights)
        logger.info("room=%s action=%s lights=%d", room_key, action, len(room_lights))
    except Exception:
        logger.error("reason=dispatcher_action_failed operation=apply action=%s", lights.diagnostic_action(action))
        _record(audit_context, "light.action_summary", "failed", "dispatcher_error", room_key, action, action_id,
                counts={})
        return
    if not isinstance(results, list):
        results = None  # a backend that reports nothing proves nothing
    _report(audit_context, room_key, action, action_id, results)
    DISPATCH_ACTIONS_TOTAL.labels(room=room_key, action=action).inc()
