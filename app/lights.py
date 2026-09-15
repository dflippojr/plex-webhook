"""Light control backends.

Two brands, each with a hybrid local-first / cloud-fallback strategy:

- Govee: try LAN control (UDP) first, fall back to the Govee Cloud API.
- Tuya (Gosund-based smart plugs/bulbs): try local control via `tinytuya`
  first, fall back to the Tuya Cloud API.

Credentials come from two places (see SETUP.md):
- Environment variables (`.env`, loaded via docker-compose `env_file`):
  GOVEE_API_KEY, TUYA_ACCESS_ID, TUYA_ACCESS_KEY.
- config/devices.secrets.yaml (gitignored): per-device local_key/ip for
  Tuya devices, and optionally a static ip for Govee devices whose LAN
  discovery isn't reliable. See config/devices.secrets.yaml.example.

Every public apply() call is defensive: missing credentials, unreachable
devices, unknown device ids, and library/network errors are all caught
and logged as warnings. Nothing here may raise - the dispatcher does not
catch exceptions from apply_action(), and this module must keep working
with zero real devices configured (the case today).
"""

import json
import logging
import os
import socket
import time
from pathlib import Path

import yaml

logger = logging.getLogger("plex-webhook")

# --- tunables -----------------------------------------------------------

DIM_BRIGHTNESS_PERCENT = 20
RESTORE_BRIGHTNESS_PERCENT = 100

GOVEE_LAN_SCAN_PORT = 4001          # multicast scan request target port
GOVEE_LAN_SCAN_MULTICAST_ADDR = "239.255.255.250"
GOVEE_LAN_LISTEN_PORT = 4002        # devices reply here
GOVEE_LAN_CONTROL_PORT = 4003       # unicast control commands go here
GOVEE_LAN_SCAN_TIMEOUT_S = 2.0

GOVEE_CLOUD_API_URL = "https://developer-api.govee.com/v1/devices/control"

SECRETS_PATH = Path(os.environ.get("DEVICES_SECRETS_PATH", "/config/devices.secrets.yaml"))


# --- secrets loading (cached) --------------------------------------------

_secrets_cache = None


def _load_secrets() -> dict:
    """Load config/devices.secrets.yaml, cached after first read.

    Missing file is expected (no credentials gathered yet) and just yields
    an empty mapping - not an error.
    """
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache

    if not SECRETS_PATH.exists():
        logger.info("no devices secrets file at %s (no local-control credentials configured yet)", SECRETS_PATH)
        _secrets_cache = {}
        return _secrets_cache

    try:
        with SECRETS_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _secrets_cache = data.get("devices", {}) or {}
    except Exception:
        logger.warning("failed to load %s - proceeding with no local-control credentials", SECRETS_PATH, exc_info=True)
        _secrets_cache = {}

    return _secrets_cache


def _device_secret(device_id: str) -> dict:
    return _load_secrets().get(device_id, {}) or {}


# --- Govee ----------------------------------------------------------------


class GoveeController:
    """Hybrid Govee control: LAN first, Govee Cloud API as fallback.

    LAN protocol (unverified against a real device - no Govee hardware
    available to test against; based on the reverse-engineered protocol
    used by community projects such as `govee_led_wez` / `python-govee-led`
    / `govee-lan-api`):

    1. Broadcast a UDP scan request to 239.255.255.250:4001:
       {"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}
    2. Devices reply on UDP port 4002 with JSON containing their `ip` and
       `device` (MAC-like id) under a `scan` response.
    3. Send unicast JSON control commands to the device's ip on port 4003,
       e.g. turn:
       {"msg": {"cmd": "turn", "data": {"value": 1}}}
       and brightness:
       {"msg": {"cmd": "brightness", "data": {"value": <1-100>}}}

    If a static `ip` is known for a device (config/devices.secrets.yaml),
    that is used directly and the discovery broadcast is skipped.
    """

    def apply(self, action: str, light: dict):
        try:
            self._apply(action, light)
        except Exception:
            logger.warning(
                "govee: unexpected error handling action=%s id=%s name=%s",
                action, light.get("id"), light.get("name"), exc_info=True,
            )

    def _apply(self, action: str, light: dict):
        device_id = light.get("id")
        name = light.get("name", device_id)
        brightness = DIM_BRIGHTNESS_PERCENT if action == "dim" else RESTORE_BRIGHTNESS_PERCENT

        if self._try_lan(light, brightness):
            logger.info("govee[lan]: %s -> on, brightness=%d%% (%s)", name, brightness, device_id)
            return

        if self._try_cloud(light, brightness):
            logger.info("govee[cloud]: %s -> on, brightness=%d%% (%s)", name, brightness, device_id)
            return

        logger.warning(
            "govee: could not control light id=%s name=%s via LAN or cloud (no reachable device / no API key configured)",
            device_id, name,
        )

    def _try_lan(self, light: dict, brightness: int) -> bool:
        device_id = light.get("id")
        secret = _device_secret(device_id)
        ip = secret.get("ip")

        if not ip:
            ip = self._discover_ip(device_id)

        if not ip:
            return False

        try:
            self._send_lan_command(ip, {"msg": {"cmd": "turn", "data": {"value": 1}}})
            self._send_lan_command(ip, {"msg": {"cmd": "brightness", "data": {"value": brightness}}})
            return True
        except Exception as exc:
            logger.info("govee[lan]: control failed for id=%s ip=%s (%s) - will try cloud fallback", device_id, ip, exc)
            return False

    def _discover_ip(self, device_id: str) -> str | None:
        """Best-effort UDP multicast discovery. Returns None on any failure."""
        if not device_id:
            return None

        request = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode("utf-8")

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", GOVEE_LAN_LISTEN_PORT))
            sock.settimeout(GOVEE_LAN_SCAN_TIMEOUT_S)
            sock.sendto(request, (GOVEE_LAN_SCAN_MULTICAST_ADDR, GOVEE_LAN_SCAN_PORT))

            deadline = time.monotonic() + GOVEE_LAN_SCAN_TIMEOUT_S
            while time.monotonic() < deadline:
                try:
                    data, _addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                try:
                    reply = json.loads(data.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                payload = (reply.get("msg") or {}).get("data") or {}
                if payload.get("device") == device_id and payload.get("ip"):
                    return payload["ip"]
        except OSError as exc:
            logger.info("govee[lan]: discovery unavailable (%s)", exc)
            return None
        finally:
            if sock is not None:
                sock.close()

        return None

    def _send_lan_command(self, ip: str, message: dict):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(1.5)
            sock.sendto(json.dumps(message).encode("utf-8"), (ip, GOVEE_LAN_CONTROL_PORT))
        finally:
            sock.close()

    def _try_cloud(self, light: dict, brightness: int) -> bool:
        api_key = os.environ.get("GOVEE_API_KEY")
        if not api_key:
            logger.info("govee[cloud]: GOVEE_API_KEY not set, skipping cloud fallback")
            return False

        device_id = light.get("id")
        model = light.get("model")
        if not model:
            logger.warning(
                "govee[cloud]: light id=%s has no 'model' (SKU) configured in rooms.yaml - required for cloud control",
                device_id,
            )
            return False

        try:
            import requests
        except ImportError:
            logger.warning("govee[cloud]: 'requests' package not available")
            return False

        headers = {"Govee-API-Key": api_key, "Content-Type": "application/json"}

        try:
            turn_body = {"device": device_id, "model": model, "cmd": {"name": "turn", "value": "on"}}
            resp = requests.put(GOVEE_CLOUD_API_URL, headers=headers, json=turn_body, timeout=5)
            resp.raise_for_status()

            brightness_body = {
                "device": device_id,
                "model": model,
                "cmd": {"name": "brightness", "value": brightness},
            }
            resp = requests.put(GOVEE_CLOUD_API_URL, headers=headers, json=brightness_body, timeout=5)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("govee[cloud]: control request failed for id=%s (%s)", device_id, exc)
            return False


# --- Tuya -------------------------------------------------------------------


class TuyaController:
    """Hybrid Tuya/Gosund control: local (tinytuya) first, Tuya Cloud API fallback.

    Local control needs, per device, an id/local_key/ip - obtained via
    `python -m tinytuya wizard` and stored in config/devices.secrets.yaml
    (never in the public rooms.yaml). Cloud fallback needs a Tuya IoT
    Platform access id/secret (TUYA_ACCESS_ID / TUYA_ACCESS_KEY) and uses
    the same device id.
    """

    def apply(self, action: str, light: dict):
        try:
            self._apply(action, light)
        except Exception:
            logger.warning(
                "tuya: unexpected error handling action=%s id=%s name=%s",
                action, light.get("id"), light.get("name"), exc_info=True,
            )

    def _apply(self, action: str, light: dict):
        device_id = light.get("id")
        name = light.get("name", device_id)
        brightness = DIM_BRIGHTNESS_PERCENT if action == "dim" else RESTORE_BRIGHTNESS_PERCENT

        if self._try_local(light, brightness):
            logger.info("tuya[local]: %s -> on, brightness=%d%% (%s)", name, brightness, device_id)
            return

        if self._try_cloud(light, brightness):
            logger.info("tuya[cloud]: %s -> on, brightness=%d%% (%s)", name, brightness, device_id)
            return

        logger.warning(
            "tuya: could not control light id=%s name=%s via local or cloud (no local key configured / no cloud credentials / device unreachable)",
            device_id, name,
        )

    def _try_local(self, light: dict, brightness: int) -> bool:
        device_id = light.get("id")
        secret = _device_secret(device_id)
        local_key = secret.get("local_key")
        ip = secret.get("ip")

        if not local_key or not ip:
            logger.info("tuya[local]: no local_key/ip configured for id=%s in devices.secrets.yaml", device_id)
            return False

        try:
            import tinytuya
        except ImportError:
            logger.warning("tuya[local]: 'tinytuya' package not available")
            return False

        try:
            device = tinytuya.OutletDevice(device_id, ip, local_key)
            device.set_version(3.3)
            device.set_socketTimeout(3)
            device.turn_on()
            # DPS 3 is the conventional brightness dp for Tuya dimmers/bulbs;
            # scale is 10-1000 on most firmware.
            device.set_value(3, int(brightness * 10))
            return True
        except Exception as exc:
            logger.info("tuya[local]: control failed for id=%s ip=%s (%s) - will try cloud fallback", device_id, ip, exc)
            return False

    def _try_cloud(self, light: dict, brightness: int) -> bool:
        access_id = os.environ.get("TUYA_ACCESS_ID")
        access_key = os.environ.get("TUYA_ACCESS_KEY")
        if not access_id or not access_key:
            logger.info("tuya[cloud]: TUYA_ACCESS_ID/TUYA_ACCESS_KEY not set, skipping cloud fallback")
            return False

        device_id = light.get("id")

        try:
            import tinytuya
        except ImportError:
            logger.warning("tuya[cloud]: 'tinytuya' package not available")
            return False

        try:
            cloud = tinytuya.Cloud(apiRegion="us", apiKey=access_id, apiSecret=access_key)
            cloud.sendcommand(device_id, [{"code": "switch_1", "value": True}])
            cloud.sendcommand(device_id, [{"code": "bright_value_v2", "value": int(brightness * 10)}])
            return True
        except Exception as exc:
            logger.warning("tuya[cloud]: control request failed for id=%s (%s)", device_id, exc)
            return False


# --- dispatch table ---------------------------------------------------------


class NullController:
    """Fallback for an unknown/misconfigured brand - logs and does nothing."""

    def apply(self, action: str, light: dict):
        logger.warning(
            "no controller for brand=%s action=%s id=%s name=%s",
            light.get("brand"), action, light.get("id"), light.get("name"),
        )


_controllers = {
    "govee": GoveeController(),
    "tuya": TuyaController(),
}
_null_controller = NullController()


def get_controller(brand: str):
    return _controllers.get(brand, _null_controller)


def apply_action(action: str, lights: list):
    for light in lights:
        controller = get_controller(light.get("brand"))
        controller.apply(action, light)
