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
  Live room mapping (Plex client UUIDs, device ids) is local
  config/rooms.yaml, also gitignored; start from rooms.yaml.example.

Every public apply() call is defensive: missing credentials, unreachable
devices, unknown device ids, and library/network errors are all caught
and logged as warnings. Nothing here may raise, and this module must keep
working with zero real devices configured (the case today).

apply() returns a LightResult instead of None so callers can audit what
happened. Outcomes are transport evidence only, never observed bulb state:

- command_sent: UDP datagrams left this host (Govee LAN has no acknowledgement)
- request_accepted_by_transport: the local device or cloud API answered without error
- unconfirmed: the call returned but nothing establishes acceptance
- failed / skipped: an attempt errored, or could not be tried at all

Results carry fixed reason codes, transport names and credential-source kinds
only. Never put keys, tokens, library responses or exception text in them.
"""

import json
import logging
import os
import socket
import time
from dataclasses import dataclass
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


SENT_OUTCOMES = ("command_sent", "request_accepted_by_transport", "unconfirmed")


@dataclass(frozen=True)
class Attempt:
    """One transport try. Truthy when it ended the light's work (no fallback needed)."""

    transport: str
    outcome: str
    reason: str
    credential_source: str = "none"
    progress: tuple = ()

    def __bool__(self):
        return self.outcome in SENT_OUTCOMES


@dataclass(frozen=True)
class LightResult:
    brand: str
    target_id: str | None
    transport: str
    credential_source: str
    outcome: str
    reason: str
    progress: tuple = ()
    attempts: tuple = ()


def brightness_for(action: str):
    """Brightness percent an action will command, or None for unknown actions."""
    if action == "dim":
        return DIM_BRIGHTNESS_PERCENT
    if action == "restore":
        return RESTORE_BRIGHTNESS_PERCENT
    return None


def _attempt_of(value, transport):
    """Accept the legacy bool a patched/old transport step may return."""
    if isinstance(value, Attempt):
        return value
    if value:
        return Attempt(transport, "unconfirmed", "result_unconfirmed")
    return Attempt(transport, "failed", "unexpected_error")


def _result(brand: str, light: dict, attempts: list) -> LightResult:
    """Collapse attempts: the terminal success, else the last failed attempt, else the last skip."""
    final = next((a for a in reversed(attempts) if a), None)
    if final is None:
        failed = [a for a in attempts if a.outcome == "failed"]
        final = (failed or attempts)[-1]
    return LightResult(brand, light.get("id"), final.transport, final.credential_source, final.outcome,
                       final.reason, final.progress, tuple(attempts))


def _brand_of(light: dict) -> str:
    brand = light.get("brand")
    return brand if isinstance(brand, str) and brand else "unknown"


def _fallback_result(brand: str, light: dict, outcome: str, reason: str) -> LightResult:
    attempt = Attempt("none", outcome, reason)
    return LightResult(brand, light.get("id"), "none", "none", outcome, reason, (), (attempt,))


def diagnostic_action(action: str) -> str:
    """Only known actions belong in failure diagnostics."""
    return action if action in ("dim", "restore") else "unknown"


def _load_secrets() -> dict:
    """Load config/devices.secrets.yaml, cached after first read.

    Missing file is expected (no credentials gathered yet) and just yields
    an empty mapping - not an error.
    """
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache

    try:
        if not SECRETS_PATH.exists():
            logger.info("reason=secrets_missing operation=load_secrets fallback=no_local_credentials")
            _secrets_cache = {}
            return _secrets_cache
        with SECRETS_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _secrets_cache = data.get("devices", {}) or {}
    except Exception:
        logger.warning("reason=secrets_load_failed operation=load_secrets fallback=no_local_credentials")
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

    def apply(self, action: str, light: dict) -> LightResult:
        try:
            result = self._apply(action, light)
            if isinstance(result, LightResult):
                return result
            return _fallback_result("govee", light, "unconfirmed", "result_unconfirmed")
        except Exception:
            logger.warning(
                "reason=unexpected_controller_error brand=govee operation=apply action=%s id=%s",
                diagnostic_action(action), light.get("id"),
            )
            return _fallback_result("govee", light, "failed", "unexpected_error")

    def _apply(self, action: str, light: dict) -> LightResult:
        device_id = light.get("id")
        name = light.get("name", device_id)
        brightness = DIM_BRIGHTNESS_PERCENT if action == "dim" else RESTORE_BRIGHTNESS_PERCENT

        attempts = []
        for transport, step in (("lan", self._try_lan), ("cloud", self._try_cloud)):
            attempt = _attempt_of(step(light, brightness), transport)
            attempts.append(attempt)
            if attempt:
                logger.info("govee[%s]: %s -> on, brightness=%d%% (%s)", transport, name, brightness, device_id)
                return _result("govee", light, attempts)

        logger.warning(
            "reason=control_unavailable brand=govee operation=apply id=%s fallback=exhausted (could not control light)",
            device_id,
        )
        return _result("govee", light, attempts)

    def _try_lan(self, light: dict, brightness: int) -> Attempt:
        device_id = light.get("id")
        secret = _device_secret(device_id)
        ip = secret.get("ip")
        source = "device_config" if ip else "none"

        if not ip:
            ip = self._discover_ip(device_id)

        if not ip:
            return Attempt("lan", "skipped", "no_address")

        progress = []
        try:
            self._send_lan_command(ip, {"msg": {"cmd": "turn", "data": {"value": 1}}})
            progress.append("turn")
            self._send_lan_command(ip, {"msg": {"cmd": "brightness", "data": {"value": brightness}}})
            progress.append("brightness")
            # UDP has no acknowledgement: the datagrams were sent, nothing more is known.
            return Attempt("lan", "command_sent", "lan_command_sent", source, tuple(progress))
        except Exception:
            logger.info("reason=local_control_failed brand=govee operation=lan_control id=%s fallback=cloud", device_id)
            return Attempt("lan", "failed", "send_error", source, tuple(progress))

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
        except OSError:
            logger.info("reason=discovery_failed brand=govee operation=lan_discovery id=%s fallback=cloud", device_id)
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

    def _try_cloud(self, light: dict, brightness: int) -> Attempt:
        api_key = os.environ.get("GOVEE_API_KEY")
        if not api_key:
            logger.info("govee[cloud]: GOVEE_API_KEY not set, skipping cloud fallback")
            return Attempt("cloud", "skipped", "missing_credentials")

        device_id = light.get("id")
        model = light.get("model")
        if not model:
            logger.warning(
                "govee[cloud]: light id=%s has no 'model' (SKU) configured in rooms.yaml - required for cloud control",
                device_id,
            )
            return Attempt("cloud", "skipped", "missing_model", "environment")

        try:
            import requests
        except ImportError:
            logger.warning("govee[cloud]: 'requests' package not available")
            return Attempt("cloud", "skipped", "library_unavailable", "environment")

        headers = {"Govee-API-Key": api_key, "Content-Type": "application/json"}

        progress = []
        try:
            turn_body = {"device": device_id, "model": model, "cmd": {"name": "turn", "value": "on"}}
            resp = requests.put(GOVEE_CLOUD_API_URL, headers=headers, json=turn_body, timeout=5)
            resp.raise_for_status()
            progress.append("turn")

            brightness_body = {
                "device": device_id,
                "model": model,
                "cmd": {"name": "brightness", "value": brightness},
            }
            resp = requests.put(GOVEE_CLOUD_API_URL, headers=headers, json=brightness_body, timeout=5)
            resp.raise_for_status()
            progress.append("brightness")
            # HTTP completion shows the API took the request, not that the bulb changed.
            return Attempt("cloud", "request_accepted_by_transport", "cloud_accepted", "environment", tuple(progress))
        except Exception:
            logger.warning("reason=cloud_control_failed brand=govee operation=cloud_control id=%s", device_id)
            return Attempt("cloud", "failed", "request_failed", "environment", tuple(progress))


# --- Tuya -------------------------------------------------------------------


class TuyaController:
    """Hybrid Tuya/Gosund control: local (tinytuya) first, Tuya Cloud API fallback.

    Local control needs, per device, an id/local_key/ip - obtained via
    `python -m tinytuya wizard` and stored in config/devices.secrets.yaml
    (never in the public rooms.yaml). Cloud fallback needs a Tuya IoT
    Platform access id/secret (TUYA_ACCESS_ID / TUYA_ACCESS_KEY) and uses
    the same device id.
    """

    def apply(self, action: str, light: dict) -> LightResult:
        try:
            result = self._apply(action, light)
            if isinstance(result, LightResult):
                return result
            return _fallback_result("tuya", light, "unconfirmed", "result_unconfirmed")
        except Exception:
            logger.warning(
                "reason=unexpected_controller_error brand=tuya operation=apply action=%s id=%s",
                diagnostic_action(action), light.get("id"),
            )
            return _fallback_result("tuya", light, "failed", "unexpected_error")

    def _apply(self, action: str, light: dict) -> LightResult:
        device_id = light.get("id")
        name = light.get("name", device_id)
        brightness = DIM_BRIGHTNESS_PERCENT if action == "dim" else RESTORE_BRIGHTNESS_PERCENT

        attempts = []
        for transport, step in (("local", self._try_local), ("cloud", self._try_cloud)):
            attempt = _attempt_of(step(light, brightness), transport)
            attempts.append(attempt)
            if attempt:
                logger.info("tuya[%s]: %s -> on, brightness=%d%% (%s)", transport, name, brightness, device_id)
                return _result("tuya", light, attempts)

        logger.warning(
            "reason=control_unavailable brand=tuya operation=apply id=%s fallback=exhausted (could not control light)",
            device_id,
        )
        return _result("tuya", light, attempts)

    @staticmethod
    def _judge(response) -> str:
        """Classify a tinytuya return: rejected, accepted, or unconfirmed. Never keeps the response."""
        if not isinstance(response, dict):
            return "unconfirmed"
        if response.get("Error") or response.get("Err") or response.get("success") is False:
            return "rejected"
        if response.get("success") is True or "dps" in response:
            return "accepted"
        return "unconfirmed"

    def _commands(self, send_turn, send_brightness, transport: str, source: str) -> Attempt:
        """Run turn then brightness, judging each return; stop at the first explicit rejection."""
        progress = []
        verdicts = []
        for step, send in (("turn", send_turn), ("brightness", send_brightness)):
            verdict = self._judge(send())
            if verdict == "rejected":
                return Attempt(transport, "failed", "tuya_rejected", source, tuple(progress))
            progress.append(step)
            verdicts.append(verdict)
        if all(v == "accepted" for v in verdicts):
            return Attempt(transport, "request_accepted_by_transport", f"{transport}_accepted", source, tuple(progress))
        return Attempt(transport, "unconfirmed", "result_unconfirmed", source, tuple(progress))

    def _try_local(self, light: dict, brightness: int) -> Attempt:
        device_id = light.get("id")
        secret = _device_secret(device_id)
        local_key = secret.get("local_key")
        ip = secret.get("ip")

        if not local_key or not ip:
            logger.info("tuya[local]: no local_key/ip configured for id=%s in devices.secrets.yaml", device_id)
            return Attempt("local", "skipped", "missing_credentials", "device_config")

        try:
            import tinytuya
        except ImportError:
            logger.warning("tuya[local]: 'tinytuya' package not available")
            return Attempt("local", "skipped", "library_unavailable", "device_config")

        try:
            device = tinytuya.OutletDevice(device_id, ip, local_key)
            device.set_version(3.3)
            device.set_socketTimeout(3)
            # DPS 3 is the conventional brightness dp for Tuya dimmers/bulbs;
            # scale is 10-1000 on most firmware.
            return self._commands(device.turn_on, lambda: device.set_value(3, int(brightness * 10)),
                                  "local", "device_config")
        except Exception:
            logger.info("reason=local_control_failed brand=tuya operation=local_control id=%s fallback=cloud", device_id)
            return Attempt("local", "failed", "send_error", "device_config")

    def _try_cloud(self, light: dict, brightness: int) -> Attempt:
        access_id = os.environ.get("TUYA_ACCESS_ID")
        access_key = os.environ.get("TUYA_ACCESS_KEY")
        if not access_id or not access_key:
            logger.info("tuya[cloud]: TUYA_ACCESS_ID/TUYA_ACCESS_KEY not set, skipping cloud fallback")
            return Attempt("cloud", "skipped", "missing_credentials")

        device_id = light.get("id")

        try:
            import tinytuya
        except ImportError:
            logger.warning("tuya[cloud]: 'tinytuya' package not available")
            return Attempt("cloud", "skipped", "library_unavailable", "environment")

        try:
            cloud = tinytuya.Cloud(apiRegion="us", apiKey=access_id, apiSecret=access_key)
            return self._commands(
                lambda: cloud.sendcommand(device_id, [{"code": "switch_1", "value": True}]),
                lambda: cloud.sendcommand(device_id, [{"code": "bright_value_v2", "value": int(brightness * 10)}]),
                "cloud", "environment")
        except Exception:
            logger.warning("reason=cloud_control_failed brand=tuya operation=cloud_control id=%s", device_id)
            return Attempt("cloud", "failed", "request_failed", "environment")


# --- dispatch table ---------------------------------------------------------


class NullController:
    """Fallback for an unknown/misconfigured brand - logs and does nothing."""

    def apply(self, action: str, light: dict) -> LightResult:
        logger.warning(
            "reason=controller_unavailable brand=unknown operation=apply action=%s id=%s",
            diagnostic_action(action), light.get("id"),
        )
        return _fallback_result(_brand_of(light), light, "skipped", "unsupported_brand")


_controllers = {
    "govee": GoveeController(),
    "tuya": TuyaController(),
}
_null_controller = NullController()


def get_controller(brand: str):
    return _controllers.get(brand, _null_controller)


def apply_action(action: str, lights: list) -> list:
    """Apply to every light in order; one failure never stops the rest. Returns LightResults."""
    results = []
    for light in lights:
        try:
            result = get_controller(light.get("brand")).apply(action, light)
        except Exception:
            result = None
        if not isinstance(result, LightResult):
            result = _fallback_result(_brand_of(light), light, "failed", "unexpected_error")
        results.append(result)
    return results
