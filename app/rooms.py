import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger("plex-webhook")
CONFIG_PATH = Path(os.environ.get("ROOMS_CONFIG_PATH", "/config/rooms.yaml"))


DEFAULT_DIM_PERCENT = 20
DEFAULT_RESTORE_PERCENT = 100
# A play with no matching pause/stop and no usable media duration stops counting after this long.
DEFAULT_PLAYBACK_IDLE_MINUTES = 6 * 60
MAX_PLAYBACK_IDLE_MINUTES = 7 * 24 * 60


class RoomConfigError(ValueError):
    """Only sanitized paths and codes may cross the API/logging boundary."""

    def __init__(self, detail):
        self.detail = detail
        super().__init__("Invalid room configuration")


class RoomConfigUnavailable(OSError):
    pass


def _key_path(path, key):
    # Room names locate errors; arbitrary extension keys are redacted.
    if path == "rooms" and isinstance(key, str):
        return f"rooms.{key}"
    if key in ("rooms", "plex_clients", "lights", "uuid", "title", "brand", "id", "name", "model",
               "dim_brightness_percent", "restore_brightness_percent", "switch_dp", "brightness_dp",
               "automations", "trigger", "room", "event", "target_room", "action",
               "allowed_server_uuids", "playback_idle_minutes"):
        return str(key) if path == "$" else f"{path}.{key}"
    return f"{path}.[key]"


def _check_yaml_keys(loader, node, path="$", ancestors=()):
    """Check explicit keys before SafeLoader can silently overwrite them."""
    if node in ancestors:
        raise RoomConfigError([{"path": path, "code": "recursive_yaml"}])
    ancestors = (*ancestors, node)
    if isinstance(node, yaml.MappingNode):
        keys = set()
        for key_node, value_node in node.value:
            # SafeLoader handles merges after checking the explicit keys.
            key = key_node.value if key_node.tag == "tag:yaml.org,2002:merge" else loader.construct_object(key_node)
            child = _key_path(path, key)
            if key in keys:
                raise RoomConfigError([{"path": child, "code": "duplicate_key"}])
            keys.add(key)
            _check_yaml_keys(loader, value_node, child, ancestors)
    elif isinstance(node, yaml.SequenceNode):
        for index, child_node in enumerate(node.value):
            _check_yaml_keys(loader, child_node, f"{path}[{index}]", ancestors)


def _load_yaml(source):
    loader = None
    try:
        loader = yaml.SafeLoader(source)
        node = loader.get_single_node()
        if node is None:
            return {}
        _check_yaml_keys(loader, node)
        return loader.construct_document(node)
    except RoomConfigError:
        raise
    except (yaml.YAMLError, TypeError, ValueError, RecursionError):
        raise RoomConfigError([{"path": "$", "code": "invalid_yaml"}]) from None
    finally:
        if loader is not None:
            loader.dispose()


def _mapping(value, path):
    if not isinstance(value, dict):
        raise RoomConfigError([{"path": path, "code": "expected_mapping"}])


def _entries(room, field, path):
    entries = room.get(field, [])
    if not isinstance(entries, list):
        raise RoomConfigError([{"path": f"{path}.{field}", "code": "expected_list"}])
    for index, entry in enumerate(entries):
        entry_path = f"{path}.{field}[{index}]"
        _mapping(entry, entry_path)
        yield entry, entry_path


def _string(entry, field, path, required=False):
    value = entry.get(field)
    valid = isinstance(value, str) and (not required or bool(value.strip()))
    if not valid and (required or value is not None):
        code = "expected_nonempty_string" if required else "expected_string_or_null"
        raise RoomConfigError([{"path": f"{path}.{field}", "code": code}])
    return value


def _integer(entry, field, path, low, high):
    """Optional whole number in [low, high]; booleans are not numbers here."""
    value = entry.get(field)
    if field in entry and (type(value) is not int or not low <= value <= high):
        raise RoomConfigError([{"path": f"{path}.{field}", "code": "expected_percent" if high == 100 else "expected_integer"}])


ONE_PLAYER_HINT = "Create one room per Plex player and put each light in the room it belongs to."


def _index_client(index, origins, value, room_key, path):
    if not value:
        return
    if value in index and index[value] != room_key:
        raise RoomConfigError([
            {"path": origins[value], "code": "conflicting_client"},
            {"path": path, "code": "conflicting_client"},
        ])
    index[value] = room_key
    origins.setdefault(value, path)


def _validate_automation(entry, path, rooms):
    _mapping(entry, path)
    trigger = entry.get("trigger")
    _mapping(trigger, f"{path}.trigger")
    source = _string(trigger, "room", f"{path}.trigger", required=True)
    target = _string(entry, "target_room", path, required=True)
    for room, field in ((source, "trigger.room"), (target, "target_room")):
        if room not in rooms:
            raise RoomConfigError([{"path": f"{path}.{field}", "code": "unknown_room"}])
    if source == target:
        raise RoomConfigError([{"path": f"{path}.target_room", "code": "self_target"}])
    if trigger.get("event") not in ("dim", "restore"):
        raise RoomConfigError([{"path": f"{path}.trigger.event", "code": "expected_lifecycle_event"}])
    action = entry.get("action")
    if action != "restore" and (type(action) is not int or not 1 <= action <= 100):
        raise RoomConfigError([{"path": f"{path}.action", "code": "expected_automation_action"}])


def _validate_automations(data, rooms):
    entries = data.get("automations", [])
    if not isinstance(entries, list):
        raise RoomConfigError([{"path": "automations", "code": "expected_list"}])
    edges = {room: set() for room in rooms}
    for index, entry in enumerate(entries):
        _validate_automation(entry, f"automations[{index}]", rooms)
        edges[entry["trigger"]["room"]].add(entry["target_room"])
    # Topological traversal rejects cycles across all trigger types, without recursion.
    indegree = dict.fromkeys(rooms, 0)
    for targets in edges.values():
        for target in targets:
            indegree[target] += 1
    ready = [room for room, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        visited += 1
        for target in edges[ready.pop()]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(rooms):
        raise RoomConfigError([{"path": "automations", "code": "automation_cycle"}])
    return entries


def _validate_options(data):
    """Top-level service options; an absent or null allow-list accepts every server."""
    minutes = data.get("playback_idle_minutes", DEFAULT_PLAYBACK_IDLE_MINUTES)
    if type(minutes) is not int or not 1 <= minutes <= MAX_PLAYBACK_IDLE_MINUTES:
        raise RoomConfigError([{"path": "playback_idle_minutes", "code": "expected_integer"}])
    servers = data.get("allowed_server_uuids")
    if servers is not None:
        # An empty list would silently ignore every event, so it is rejected rather than honored.
        if not isinstance(servers, list) or not servers:
            raise RoomConfigError([{"path": "allowed_server_uuids", "code": "expected_nonempty_list"}])
        for index, value in enumerate(servers):
            if not isinstance(value, str) or not value.strip():
                raise RoomConfigError([{"path": f"allowed_server_uuids[{index}]", "code": "expected_nonempty_string"}])
        servers = frozenset(servers)
    return {"playback_idle_minutes": minutes, "allowed_server_uuids": servers}


def _validate(data):
    _mapping(data, "$")
    rooms = data.get("rooms", {})
    _mapping(rooms, "rooms")
    uuid_index, title_index = {}, {}
    uuid_origins, title_origins, light_origins = {}, {}, {}
    for room_key, room in rooms.items():
        if not isinstance(room_key, str):
            raise RoomConfigError([{"path": "rooms.[key]", "code": "expected_string"}])
        path = f"rooms.{room_key}"
        _mapping(room, path)
        clients = list(_entries(room, "plex_clients", path))
        for field in ("dim_brightness_percent", "restore_brightness_percent"):
            _integer(room, field, path, 1, 100)
        for client, client_path in clients:
            uuid = _string(client, "uuid", client_path)
            title = _string(client, "title", client_path)
            _index_client(uuid_index, uuid_origins, uuid, room_key, f"{client_path}.uuid")
            normalized = title.strip().lower() if title else None
            _index_client(title_index, title_origins, normalized, room_key, f"{client_path}.title")
        for light, light_path in _entries(room, "lights", path):
            for field in ("brand", "id"):
                _string(light, field, light_path, required=True)
            for field in ("name", "model"):
                _string(light, field, light_path)
            for field in ("switch_dp", "brightness_dp"):
                _integer(light, field, light_path, 1, 255)
            light_id = light["id"]
            if light_id in light_origins:
                raise RoomConfigError([
                    {"path": light_origins[light_id], "code": "light_in_multiple_rooms",
                     "hint": "A light belongs to exactly one room; give each room its own lights."},
                    {"path": f"{light_path}.id", "code": "light_in_multiple_rooms"},
                ])
            light_origins[light_id] = f"{light_path}.id"
        if len(clients) != 1:
            raise RoomConfigError([{"path": f"{path}.plex_clients", "code": "expected_one_player", "hint": ONE_PLAYER_HINT}])
    return rooms, uuid_index, title_index, _validate_automations(data, rooms), _validate_options(data)


EMPTY_STATE = ({}, {}, {}, [], _validate_options({}))


class RoomRegistry:
    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path
        self._state = EMPTY_STATE
        self.load_outcome, self.load_reason = "rejected", "config_unavailable"
        try:
            self.reload()
        except RoomConfigError as exc:
            self.load_reason = "invalid_config"
            logger.error("Invalid startup rooms config: %s", [error["code"] for error in exc.detail])
        except RoomConfigUnavailable:
            logger.warning("Rooms config unavailable; dispatcher disabled until a valid reload")

    @property
    def rooms(self):
        return self._state[0]

    def automations_for(self, room_key, event):
        return [entry for entry in self._state[3]
                if entry["trigger"]["room"] == room_key and entry["trigger"]["event"] == event]

    def validate(self):
        """Read and validate the configured file without publishing any state."""
        try:
            source = self.config_path.read_text(encoding="utf-8")
        except OSError:
            raise RoomConfigUnavailable("Rooms configuration unavailable") from None
        except UnicodeError:
            raise RoomConfigError([{"path": "$", "code": "invalid_yaml"}]) from None
        return _validate(_load_yaml(source))

    def reload(self):
        candidate = self.validate()
        # One reference publishes rooms, indexes, automations and options together.
        self._state = candidate
        self.load_outcome, self.load_reason = "activated", "loaded"
        logger.info("loaded %d room(s)", len(self.rooms))

    def resolve_room(self, payload: dict) -> str | None:
        _, uuid_index, title_index, _, _ = self._state
        player = (payload or {}).get("Player") or {}
        uuid = player.get("uuid")
        title = player.get("title")
        if uuid and uuid in uuid_index:
            return uuid_index[uuid]
        if title:
            return title_index.get(title.strip().lower())
        return None

    def player_label(self, title) -> str:
        """The configured client title this player title matches (case-insensitive, like room matching), else 'other'."""
        rooms, _, title_index, _, _ = self._state
        room = title_index.get(title.strip().lower()) if isinstance(title, str) else None
        return rooms[room]["plex_clients"][0]["title"].strip() if room else "other"

    def playback_idle_seconds(self) -> int:
        return self._state[4]["playback_idle_minutes"] * 60

    def server_allowed(self, payload: dict) -> bool:
        """True unless an allow-list is configured and the payload's ``Server.uuid`` is not on it."""
        allowed = self._state[4]["allowed_server_uuids"]
        if allowed is None:
            return True
        server = (payload or {}).get("Server")
        uuid = server.get("uuid") if isinstance(server, dict) else None
        return isinstance(uuid, str) and uuid in allowed

    def settings_for(self, room_key: str) -> dict:
        """Dim level and restore fallback in percent, with defaults for omitted fields."""
        room = self.rooms.get(room_key) or {}
        return {"dim": room.get("dim_brightness_percent", DEFAULT_DIM_PERCENT),
                "restore": room.get("restore_brightness_percent", DEFAULT_RESTORE_PERCENT)}

    def lights_for(self, room_key: str) -> list:
        return self.rooms.get(room_key, {}).get("lights", [])


registry = RoomRegistry()
