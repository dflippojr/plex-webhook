import copy
import logging
from collections import Counter as OutcomeCounter
from concurrent.futures import ThreadPoolExecutor
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

# A light further than this many percentage points from the dim level was changed by hand.
MANUAL_CHANGE_TOLERANCE_PERCENT = 5

# One worker applies every light action in the order it was queued, so a restore
# can never overtake an earlier dim for the same room, and a slow or unreachable
# light only delays later actions, never the event loop.
_action_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="light-action")

# room_key -> set of client identifiers currently playing in that room
_active_clients: dict[str, set] = {}

# Rooms that may still hold restore records: dimmed and not yet restored, including
# before a restart, when the in-memory player state is gone but the records are not.
_pending_rooms: set[str] = set()


def _load_pending_rooms():
    try:
        _pending_rooms.update(db.rooms_with_restore_records())
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
        _dispatch(room_key, "dim", audit_context)
    elif (was_active and not is_active) or (not was_active and event in DEACTIVATE_EVENTS and room_key in _pending_rooms):
        # The second case: a restart lost the player state but the dim's records survived.
        _pending_rooms.discard(room_key)
        _dispatch(room_key, "restore", audit_context)


def _dispatch(room_key: str, action: str, audit_context: AuditContext | None = None):
    """Snapshot the targets, queue the light action on the worker thread and return its Future.

    Targets are resolved now so a config reload after enqueue cannot change a queued action.
    """
    action_id = audit.correlation_id()
    try:
        room_lights = copy.deepcopy(registry.lights_for(room_key))
        settings = dict(registry.settings_for(room_key))
    except Exception:
        # Reported by the worker through the same failure path as any other action error.
        return _action_executor.submit(_apply, room_key, action, None, audit_context, action_id)
    level = settings["dim"] if action == "dim" else None  # a restore level is read per light at execution time
    _record(audit_context, "light.action_queued", "queued", "accepted", room_key, action, action_id,
            targets=[{"brand": _text(light.get("brand")), "id": _text(light.get("id")),
                      "brightness": level} for light in room_lights])
    return _action_executor.submit(_apply, room_key, action, room_lights, audit_context, action_id, settings)


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


def _apply(room_key: str, action: str, room_lights=None, audit_context=None, action_id=None, settings=None):
    action_id = action_id or audit.correlation_id()
    settings = settings or {"dim": DEFAULT_DIM_PERCENT, "restore": DEFAULT_RESTORE_PERCENT}
    try:
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
