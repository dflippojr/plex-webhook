import logging
from pathlib import Path

import yaml

logger = logging.getLogger("plex-webhook")

CONFIG_PATH = Path("/config/rooms.yaml")


class RoomRegistry:
    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path
        self.rooms: dict = {}
        self._uuid_index: dict[str, str] = {}
        self._title_index: dict[str, str] = {}
        self.reload()

    def reload(self):
        if not self.config_path.exists():
            logger.warning(
                "rooms config not found at %s - copy config/rooms.yaml.example to config/rooms.yaml (dispatcher disabled until then)",
                self.config_path,
            )
            self.rooms = {}
            self._uuid_index = {}
            self._title_index = {}
            return

        with self.config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        self.rooms = data.get("rooms", {})
        self._uuid_index = {}
        self._title_index = {}

        for room_key, room in self.rooms.items():
            for client in room.get("plex_clients", []):
                uuid = client.get("uuid")
                title = client.get("title")
                if uuid:
                    self._uuid_index[uuid] = room_key
                if title:
                    self._title_index[title.strip().lower()] = room_key

        logger.info("loaded %d room(s) from %s", len(self.rooms), self.config_path)

    def resolve_room(self, payload: dict) -> str | None:
        player = (payload or {}).get("Player") or {}
        uuid = player.get("uuid")
        title = player.get("title")

        if uuid and uuid in self._uuid_index:
            return self._uuid_index[uuid]
        if title and title.strip().lower() in self._title_index:
            return self._title_index[title.strip().lower()]
        return None

    def lights_for(self, room_key: str) -> list:
        return self.rooms.get(room_key, {}).get("lights", [])


registry = RoomRegistry()
