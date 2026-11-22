"""Service settings, read from the environment once, when the app is created.

ADMIN_API_TOKEN and the device keys (GOVEE_API_KEY, TUYA_ACCESS_ID,
TUYA_ACCESS_KEY, HASS_URL, HASS_TOKEN) are not here: they are read where they are used, so rotating
them needs no restart. Reading settings opens no file.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from app import auth


def env_int(environ, name, default):
    """A positive integer from the environment, or ``default`` when unset, malformed or not positive."""
    try:
        value = int(environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


def _read_timeout(environ):
    """Seconds a light state read may take: 0 < value <= 30, else the 2 s default."""
    try:
        value = float(environ.get("LIGHT_READ_TIMEOUT_S", "2"))
    except ValueError:
        return 2.0
    return value if 0 < value <= 30 else 2.0


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("/data/plex_events.db")
    rooms_config_path: Path = Path("/config/rooms.yaml")
    devices_secrets_path: Path = Path("/config/devices.secrets.yaml")
    # Request bounds for the unauthenticated webhook, sized for a Plex delivery.
    webhook_max_body_bytes: int = 2 * 1024 * 1024
    # The stored raw payload (events.raw_payload) is truncated here.
    raw_payload_max_bytes: int = 64 * 1024
    # Bound on queued light actions; see ActionQueue.
    dispatch_queue_limit: int = 32
    light_read_timeout_s: float = 2.0
    # Digest of WEBHOOK_TOKEN, or None when unset; only the digest is kept.
    webhook_token_digest: bytes | None = None

    @classmethod
    def from_env(cls, environ=None):
        """Settings from the environment. Raises auth.WebhookTokenError for an unusable WEBHOOK_TOKEN."""
        environ = os.environ if environ is None else environ
        defaults = cls()
        return cls(
            db_path=Path(environ.get("DB_PATH", defaults.db_path)),
            rooms_config_path=Path(environ.get("ROOMS_CONFIG_PATH", defaults.rooms_config_path)),
            devices_secrets_path=Path(environ.get("DEVICES_SECRETS_PATH", defaults.devices_secrets_path)),
            webhook_max_body_bytes=env_int(environ, "WEBHOOK_MAX_BODY_BYTES", defaults.webhook_max_body_bytes),
            raw_payload_max_bytes=env_int(environ, "RAW_PAYLOAD_MAX_BYTES", defaults.raw_payload_max_bytes),
            dispatch_queue_limit=env_int(environ, "DISPATCH_QUEUE_LIMIT", defaults.dispatch_queue_limit),
            light_read_timeout_s=_read_timeout(environ),
            webhook_token_digest=auth.webhook_token_digest(environ.get("WEBHOOK_TOKEN", "")),
        )
