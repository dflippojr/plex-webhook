import logging
from concurrent.futures import ThreadPoolExecutor

from prometheus_client import Counter, Gauge

from app import lights
from app.rooms import registry

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

# One worker applies every light action in the order it was queued, so a restore
# can never overtake an earlier dim for the same room, and a slow or unreachable
# light only delays later actions, never the event loop.
_action_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="light-action")

# room_key -> set of client identifiers currently playing in that room
_active_clients: dict[str, set] = {}


def init_room_gauges():
    """Export every configured room so dashboards see 0 after a restart, not a stale pre-restart value."""
    for room_key in registry.rooms:
        ROOM_ACTIVE_SESSIONS.labels(room=room_key).set(len(_active_clients.get(room_key, ())))


init_room_gauges()


def _client_id(payload: dict) -> str:
    player = (payload or {}).get("Player") or {}
    return player.get("uuid") or player.get("title") or "unknown"


def handle_event(payload: dict):
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
        _dispatch(room_key, "dim")
    elif was_active and not is_active:
        _dispatch(room_key, "restore")


def _dispatch(room_key: str, action: str):
    """Queue the light action on the worker thread and return its Future."""
    return _action_executor.submit(_apply, room_key, action)


def _apply(room_key: str, action: str):
    try:
        room_lights = registry.lights_for(room_key)
        lights.apply_action(action, room_lights)
        logger.info("room=%s action=%s lights=%d", room_key, action, len(room_lights))
    except Exception:
        logger.error("reason=dispatcher_action_failed operation=apply action=%s", lights.diagnostic_action(action))
        return
    DISPATCH_ACTIONS_TOTAL.labels(room=room_key, action=action).inc()
