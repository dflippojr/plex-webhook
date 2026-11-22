"""Playback state: which rooms are playing, and the light actions their transitions queue.

``PlaybackState`` holds the per-room player state and turns webhook events and the
expiry sweep into dim and restore actions on its ``ActionQueue``. The service
(app.service) builds one at startup; nothing here runs at import.
"""
import copy
import logging
import math
import threading
import time
from collections import Counter as OutcomeCounter
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable

from app import audit, lights
from app.deadline import DeadlineExceeded
from app.rooms import DEFAULT_DIM_PERCENT, DEFAULT_RESTORE_PERCENT

logger = logging.getLogger("plex-webhook")

ACTIVATE_EVENTS = {"media.play", "media.resume"}
DEACTIVATE_EVENTS = {"media.pause", "media.stop"}

METRIC_BRANDS = {"govee", "tuya", "hass"}
LIGHT_OUTCOMES = set(audit.OPERATIONS["light.result"][3])

# A light further than this many percentage points from the dim level was changed by hand.
MANUAL_CHANGE_TOLERANCE_PERCENT = 5

# A play that reports its media duration expires this long after the remaining runtime.
DURATION_GRACE_SECONDS = 5 * 60
# Upper bound on a duration-based expiry, so an implausible duration cannot pin a room.
MAX_DURATION_SECONDS = 24 * 60 * 60


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


class CheckDropped(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _client_id(payload: dict) -> str:
    player = (payload or {}).get("Player") or {}
    return player.get("uuid") or player.get("title") or "unknown"


def _milliseconds(value):
    if type(value) not in (int, float):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return None
    return value if finite and value >= 0 else None


def _text(value):
    return value if isinstance(value, str) and value else "unknown"


def _record(context, name, outcome, reason, target, action, action_id, *, deadline=None, **detail):
    """Append one light record through ``context``. With a shutdown ``deadline`` the write must end by
    it; DeadlineExceeded propagates so the queue counts the record as unaudited."""
    if context is None or action not in audit.LIGHT_REQUESTS:
        return
    detail.update(action_id=action_id, request=action, on_behalf_of=context.actor, event_id=context.event_id)
    fields = dict(action=name, outcome=outcome, reason_code=reason, correlation=context.correlation,
                  target_id=target if isinstance(target, str) and target else None, detail=detail)
    if deadline is not None:
        fields["deadline"] = deadline
    try:
        context.record(**fields)
    except DeadlineExceeded:
        raise
    except Exception:
        logger.warning("reason=audit_write_failed")


def _summary(context, outcome, reason, target, action, action_id, deadline=None):
    """A terminal record for an action that touched no light."""
    _record(context, "light.action_summary", outcome, reason, target, action, action_id, deadline=deadline,
            counts={})


def _record_queued(context, room_key, action, action_id, room_lights, level):
    _record(context, "light.action_queued", "queued", "accepted", room_key, action, action_id,
            targets=[{"brand": _text(light.get("brand")), "id": _text(light.get("id")),
                      "brightness": level} for light in room_lights])


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


class PlaybackState:
    """Per-room player state and the light actions its transitions queue.

    ``registry`` is the rooms configuration, ``metrics`` the service's collectors,
    ``queue`` the one ActionQueue every light action and check runs on, and
    ``records`` the restore records in the database. ``clock`` is monotonic time for
    play expiry; tests pass a fake.
    """

    def __init__(self, registry, metrics, queue, records, clock=time.monotonic):
        self.registry = registry
        self.metrics = metrics
        # One worker applies every light action in the order it was queued, so a restore
        # can never overtake an earlier dim for the same room, and a slow or unreachable
        # light only delays later actions, never the event loop. A room's queued action is
        # replaced by a newer one for the same room, and the queue is bounded.
        self.queue = queue
        self.records = records
        self.clock = clock
        # room_key -> {client identifier: clock time after which its play is presumed over}
        self.active_clients: dict[str, dict[str, float]] = {}
        # room_key -> the unlisted uuid of the latest title-matched play, which may also end the room's
        # playback. One per room, dropped when the room's playback ends.
        self.activating_uuids: dict[str, str] = {}
        # Rooms that may still hold restore records: dimmed and not yet restored, including
        # before a restart, when the in-memory player state is gone but the records are not.
        self.pending_rooms: set[str] = set()
        # Webhook handling and the expiry sweep both change player state.
        self._lock = threading.RLock()

    def load_pending_rooms(self):
        """Rooms whose restore records or automation ownership survived a restart."""
        try:
            self.pending_rooms.update(self.records.rooms_with_restore_records())
            self.pending_rooms.update(self.records.automation_sources())
        except Exception:
            logger.warning("reason=restore_records_unavailable operation=load_pending")

    def init_room_gauges(self):
        """Export every configured room so dashboards see 0 after a restart, not a stale pre-restart value.

        A reload also drops the series of rooms it removed or renamed, so they never show a phantom session.
        """
        gauge = self.metrics.room_active_sessions
        with self._lock:
            rooms = self.registry.rooms
            exported = {sample.labels["room"] for metric in gauge.collect() for sample in metric.samples}
            for room_key in exported - rooms.keys():
                gauge.remove(room_key)
            for room_key in rooms:
                gauge.labels(room=room_key).set(len(self.active_clients.get(room_key, ())))

    def _set_room_gauge(self, room_key, active):
        """Only configured rooms get a series; a room a reload removed keeps its players but not its gauge."""
        if room_key in self.registry.rooms:
            self.metrics.room_active_sessions.labels(room=room_key).set(len(active))

    def _expires_at(self, payload: dict, now: float) -> float:
        """A few minutes past the remaining media runtime, or the configured idle period without one."""
        metadata = payload.get("Metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        duration = _milliseconds(metadata.get("duration"))
        if not duration:
            return now + self.registry.playback_idle_seconds()
        offset = _milliseconds(metadata.get("viewOffset"))
        remaining = (duration - offset if offset is not None and offset < duration else duration) / 1000
        return now + min(remaining, MAX_DURATION_SECONDS) + DURATION_GRACE_SECONDS

    def expire_idle_clients(self, context_factory: Callable | None = None):
        """Drop players whose play went stale; a room whose last player expires gets the normal restore.

        ``context_factory`` returns a fresh AuditContext (or None) for each restored room.
        """
        now = self.clock()
        with self._lock:
            for room_key, active in list(self.active_clients.items()):
                stale = [client_id for client_id, expires_at in active.items() if expires_at <= now]
                if not stale:
                    continue
                for client_id in stale:
                    del active[client_id]
                self.metrics.expired_players.labels(room=room_key).inc(len(stale))
                self._set_room_gauge(room_key, active)
                logger.info("room=%s reason=playback_expired players=%d", room_key, len(stale))
                if not active:
                    self.activating_uuids.pop(room_key, None)
                    self.pending_rooms.discard(room_key)
                    self.dispatch_lifecycle(room_key, "restore", context_factory() if context_factory else None)

    def handle_event(self, payload: dict, audit_context: AuditContext | None = None):
        if not payload:
            return

        event = payload.get("event")
        if event not in ACTIVATE_EVENTS and event not in DEACTIVATE_EVENTS:
            return

        match = self.registry.match_player(payload)
        if match is None:
            logger.debug("event=%s from unmapped client, ignoring", event)
            return
        room_key = match.room
        activates = event in ACTIVATE_EVENTS and self.registry.dims_for(room_key, payload)

        player_uuid = payload["Player"].get("uuid")
        with self._lock:
            if event in ACTIVATE_EVENTS and not activates and not self.active_clients.get(room_key):
                logger.debug("event=%s media type not in the room's dim_media_types, ignoring", event)
                return

            active = self.active_clients.setdefault(room_key, {})
            was_active = len(active) > 0

            # A room has one player, which may arrive by uuid or by title alone: it keeps one entry, under the
            # room's client label, and a stop or pause from it ends the room's playback whatever the label was.
            # An unlisted uuid ends it only if it made the room's latest title-matched play. A filtered play or
            # resume ends playback the same way: the player moved on from the item that dimmed the room, even
            # if Plex sends no stop between queue items.
            if activates:
                # Every play/resume pushes the expiry out again.
                active.clear()
                active[match.label] = self._expires_at(payload, self.clock())
                if not match.configured:
                    self.activating_uuids[room_key] = player_uuid
                elif player_uuid:  # the listed player took the room over
                    self.activating_uuids.pop(room_key, None)
            elif match.configured or self.activating_uuids.get(room_key) == player_uuid:
                active.clear()
                self.activating_uuids.pop(room_key, None)

            is_active = len(active) > 0
            self._set_room_gauge(room_key, active)

            if not was_active and is_active:
                self.pending_rooms.add(room_key)
                self.dispatch_lifecycle(room_key, "dim", audit_context)
            elif (was_active and not is_active) or (not was_active and event in DEACTIVATE_EVENTS
                                                     and room_key in self.pending_rooms):
                # The second case: a restart lost the player state but the dim's records survived.
                self.pending_rooms.discard(room_key)
                self.dispatch_lifecycle(room_key, "restore", audit_context)

    def dispatch_lifecycle(self, room_key, action, context):
        self.dispatch(room_key, action, context)
        registry = self.registry
        entries = copy.deepcopy(registry.automations_for(room_key, action))
        # Cleanup includes persisted targets, even if a reload removed their automation.
        snapshots = {room: (self._lights_snapshot(room), dict(registry.settings_for(room)))
                     for room in registry.rooms}
        blocked = {room for room in snapshots if self.active_clients.get(room)}
        # Nothing is audited for automations until they run, so a dropped batch leaves no queued record.
        self.queue.submit(self._run_automations, room_key, action, entries, snapshots, blocked, context,
                          key=("automations", room_key),
                          on_drop=lambda reason, deadline=None: self._dropped(room_key, reason))

    def _run_automations(self, source, event, entries, snapshots, blocked, context):
        """Run on the same worker as playback actions; automation actions never emit triggers."""
        try:
            if event == "restore":
                for target in sorted(self.records.automation_targets(source)):
                    self._automation_action(source, target, "restore", snapshots, blocked, context)
            for entry in entries:
                # A restore trigger's explicit restore yields to a newer source's automation on the target.
                self._automation_action(source, entry["target_room"], entry["action"], snapshots, blocked, context,
                                        yield_to_owner=event == "restore" and entry["action"] == "restore")
        except Exception:
            logger.error("reason=dispatcher_action_failed operation=automation")

    def _automation_action(self, source, target, action, snapshots, blocked, context, yield_to_owner=False):
        if target not in snapshots:
            return
        room_lights, settings = snapshots[target]
        settings = dict(settings)
        if type(action) is int:
            settings["dim"] = action
            action = "dim"
        action_id = audit.correlation_id()
        if target in blocked:
            _summary(context, "skipped", "own_playback_active", target, action, action_id)
            return
        if yield_to_owner and self.records.automation_owner(target) not in (None, source):
            _summary(context, "skipped", "owned_by_newer_source", target, action, action_id)
            return
        _record_queued(context, target, action, action_id, room_lights, settings["dim"] if action == "dim" else None)
        self.apply(target, action, room_lights, context, action_id, settings, automation_source=source)

    def check_lights(self, room_key: str) -> Future:
        """Queue a read-only check of a room's lights behind any queued light action.

        The reads share the worker because Govee LAN reads and discovery both bind UDP 4002,
        so a concurrent dim could take their replies. The Future resolves to one entry per
        configured light, or raises CheckDropped when the queue drops the check. A cancelled
        Future (a caller that gave up) is skipped if it has not started yet.
        """
        room_lights = self._lights_snapshot(room_key)
        result = Future()

        def run():
            if result.set_running_or_notify_cancel():
                try:
                    result.set_result(self._check(room_key, room_lights))
                except BaseException as exc:
                    result.set_exception(exc)

        def on_drop(reason, deadline=None):
            # Not _dropped: no light action was lost, and the caller audits the refusal.
            if result.set_running_or_notify_cancel():
                result.set_exception(CheckDropped(reason))

        self.queue.submit(run, key=("check", room_key), on_drop=on_drop, evict=False)
        return result

    def _check(self, room_key, room_lights):
        """Read each light and whether it holds a restore record. Changes no light, record or player state."""
        lights.prepare(room_lights)
        entries = []
        for light in room_lights:
            reading = lights.read_state(light)
            ok = reading.ok is True
            transport = reading.transport if ok and reading.transport in audit.LIGHT_TRANSPORTS else "none"
            brightness = reading.brightness if ok and type(reading.brightness) is int else None
            entries.append({
                "id": light.get("id"), "brand": light.get("brand"), "name": light.get("name"),
                "read": {"ok": ok, "on": reading.on if ok and type(reading.on) is bool else None,
                         "brightness_percent": brightness, "transport": transport},
                "restore_record": self.records.get_restore_record(room_key, _text(light.get("id"))) is not None,
            })
        return entries

    def dispatch(self, room_key: str, action: str, audit_context: AuditContext | None = None):
        """Snapshot the targets, queue the light action on the worker thread and return its Future.

        Targets are resolved now so a config reload after enqueue cannot change a queued action.
        """
        action_id = audit.correlation_id()

        def on_drop(reason, deadline=None):
            # The terminal record for a queued action that never ran; no light was touched.
            self._dropped(room_key, reason)
            _summary(audit_context, "skipped", reason, room_key, action, action_id, deadline)

        try:
            room_lights = self._lights_snapshot(room_key)
            settings = dict(self.registry.settings_for(room_key))
        except Exception:
            # Reported by the worker through the same failure path as any other action error.
            room_lights = settings = None
        else:
            level = settings["dim"] if action == "dim" else None  # a restore level is read per light at execution
            _record_queued(audit_context, room_key, action, action_id, room_lights, level)
        return self.queue.submit(self.apply, room_key, action, room_lights, audit_context, action_id, settings,
                                 key=("room", room_key), on_drop=on_drop)

    def _lights_snapshot(self, room_key):
        return copy.deepcopy(self.registry.lights_for(room_key))

    def _dropped(self, room_key, reason):
        self.metrics.dispatch_dropped.labels(room=room_key, reason=reason).inc()
        if reason == "queue_overflow":
            logger.warning("reason=dispatch_queue_overflow limit=%d", self.queue.limit)

    def _count_result(self, room_key, result):
        """Fixed label values only: configured unknown brands become ``other``."""
        self.metrics.light_results.labels(
            room=room_key,
            brand=result.brand if result.brand in METRIC_BRANDS else "other",
            transport=result.transport if result.transport in audit.LIGHT_TRANSPORTS else "none",
            outcome=result.outcome if result.outcome in LIGHT_OUTCOMES else "unconfirmed",
        ).inc()

    def _report(self, context, room_key, action, action_id, results):
        for result in results or ():
            self._count_result(room_key, result)
            _record(context, "light.result", result.outcome, result.reason, result.target_id, action, action_id,
                    brand=_text(result.brand), transport=result.transport,
                    credential_source=result.credential_source, progress=result.progress,
                    attempts=[{"transport": a.transport, "credential_source": a.credential_source,
                               "outcome": a.outcome, "reason_code": a.reason, "progress": a.progress}
                              for a in result.attempts])
        outcome, reason = _summarize(results) if results is not None else ("unconfirmed", "no_results")
        counts = dict(OutcomeCounter(result.outcome for result in results or ()))
        _record(context, "light.action_summary", outcome, reason, room_key, action, action_id, counts=counts)

    def _decide(self, context, room_key, light, request, action_id, decision, reason, settings, restore=None,
                observed=None):
        """One per-light brightness decision: counted always, audited when the action is."""
        self.metrics.light_decisions.labels(room=room_key, decision=decision).inc()
        _record(context, "light.decision", decision, reason, light.get("id"), request, action_id,
                brand=_text(light.get("brand")), dim_percent=settings["dim"], restore_percent=restore,
                observed_percent=observed)

    def _dim(self, room_key, room_lights, settings, context, action_id):
        """Record each light's own brightness, then dim the ones that are on. Never turns a light on.

        A light whose state could not be read may be off, so it gets the brightness command alone.
        """
        records = self.records
        dim, results, targets, unread = settings["dim"], [], [], []
        lights.prepare(room_lights)  # one Govee discovery scan serves the reads and the commands
        for light in room_lights:
            light_id = _text(light.get("id"))
            existing = records.get_restore_record(room_key, light_id)
            reading = None if existing else lights.read_state(light)
            was_off = existing["was_off"] if existing else reading.ok and not reading.on
            if was_off:
                if not existing:
                    records.put_restore_record(room_key, light_id, True, None, dim)
                self._decide(context, room_key, light, "dim", action_id, "dim_skipped_off", "was_off", settings)
                results.append(lights.skipped_result(light, "light_off"))
                continue
            if existing:  # a re-dim before the pending restore ran keeps the original value
                self._decide(context, room_key, light, "dim", action_id, "dim_kept_original", "record_exists",
                             settings, existing["restore_percent"])
                # A newer automation or the target's own playback changes the expected level,
                # while preserving the brightness from before the first dim.
                records.put_restore_record(room_key, light_id, False, existing["restore_percent"], dim)
                targets.append(light)
                continue
            restore, reason = (reading.brightness, "read_ok") if reading.ok else (settings["restore"], "read_failed")
            if restore == dim:  # a stored value equal to the dim level could never undo the dim
                restore, reason = settings["restore"], "at_dim_level"
            if restore != dim:
                records.put_restore_record(room_key, light_id, False, restore, dim)
            self._decide(context, room_key, light, "dim", action_id, "dim_recorded", reason, settings, restore,
                         reading.brightness)
            (targets if reading.ok else unread).append(light)
        applied = lights.apply_action("dim", targets, dim) if targets else []
        unread_applied = lights.apply_action("dim", unread, dim, turn_on=False) if unread else []
        if not isinstance(applied, list) or not isinstance(unread_applied, list):
            return None
        return results + applied + unread_applied

    def _restore(self, room_key, room_lights, settings, context, action_id):
        """Put each recorded light back unless it was off, or changed by hand while dimmed."""
        results, targets, levels, cleared = [], [], {}, []
        lights.prepare(room_lights)
        for light in room_lights:
            light_id = _text(light.get("id"))
            record = self.records.get_restore_record(room_key, light_id)
            if record is None:
                results.append(lights.skipped_result(light, "no_record"))
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
            cleared.append(light_id)
            self._decide(context, room_key, light, "restore", action_id, decision, reason, {**settings, "dim": dim},
                         restore, observed)
            if decision in ("restored", "restore_without_read"):
                targets.append(light)
                levels[light_id] = restore
            else:
                results.append(lights.skipped_result(light, "light_off" if record["was_off"] else "manual_change"))
        applied = lights.apply_action("restore", targets, levels) if targets else []
        # Clear records only after the commands: a failure earlier in the loop keeps every record for a retry.
        for light_id in cleared:
            self.records.delete_restore_record(room_key, light_id)
        return results + applied if isinstance(applied, list) else None

    def apply(self, room_key: str, action: str, room_lights=None, audit_context=None, action_id=None, settings=None,
              automation_source=None):
        """Run one light action on the calling thread (the queue's worker, in service)."""
        action_id = action_id or audit.correlation_id()
        settings = settings or {"dim": DEFAULT_DIM_PERCENT, "restore": DEFAULT_RESTORE_PERCENT}
        try:
            # Also check at execution: a queued automation must yield to playback that
            # started while an earlier light command was still running.
            if automation_source is not None and self.active_clients.get(room_key):
                _summary(audit_context, "skipped", "own_playback_active", room_key, action, action_id)
                return
            if action in ("dim", "restore"):
                self.records.set_automation_owner(room_key, automation_source if action == "dim" else None)
            if room_lights is None:
                room_lights = self.registry.lights_for(room_key)
            if action == "dim":
                results = self._dim(room_key, room_lights, settings, audit_context, action_id)
            elif action == "restore":
                results = self._restore(room_key, room_lights, settings, audit_context, action_id)
            else:
                results = lights.apply_action(action, room_lights)
            logger.info("room=%s action=%s lights=%d", room_key, action, len(room_lights))
        except Exception:
            logger.error("reason=dispatcher_action_failed operation=apply action=%s", lights.diagnostic_action(action))
            _summary(audit_context, "failed", "dispatcher_error", room_key, action, action_id)
            return
        if not isinstance(results, list):
            results = None  # a backend that reports nothing proves nothing
        self._report(audit_context, room_key, action, action_id, results)
        self.metrics.dispatch_actions.labels(room=room_key, action=action).inc()
