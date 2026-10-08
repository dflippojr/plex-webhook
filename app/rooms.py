import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger("plex-webhook")
CONFIG_PATH = Path(os.environ.get("ROOMS_CONFIG_PATH", "/config/rooms.yaml"))


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
    if key in ("rooms", "plex_clients", "lights", "uuid", "title", "brand", "id", "name", "model"):
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


def _validate(data):
    _mapping(data, "$")
    rooms = data.get("rooms", {})
    _mapping(rooms, "rooms")
    uuid_index, title_index = {}, {}
    uuid_origins, title_origins = {}, {}
    for room_key, room in rooms.items():
        if not isinstance(room_key, str):
            raise RoomConfigError([{"path": "rooms.[key]", "code": "expected_string"}])
        path = f"rooms.{room_key}"
        _mapping(room, path)
        for client, client_path in _entries(room, "plex_clients", path):
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
    return rooms, uuid_index, title_index


class RoomRegistry:
    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path
        self._state = ({}, {}, {})
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
        # One reference publishes the rooms and both indexes together.
        self._state = candidate
        self.load_outcome, self.load_reason = "activated", "loaded"
        logger.info("loaded %d room(s)", len(self.rooms))

    def resolve_room(self, payload: dict) -> str | None:
        _, uuid_index, title_index = self._state
        player = (payload or {}).get("Player") or {}
        uuid = player.get("uuid")
        title = player.get("title")
        if uuid and uuid in uuid_index:
            return uuid_index[uuid]
        if title:
            return title_index.get(title.strip().lower())
        return None

    def lights_for(self, room_key: str) -> list:
        return self.rooms.get(room_key, {}).get("lights", [])


registry = RoomRegistry()
