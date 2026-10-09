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
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import yaml

logger = logging.getLogger("plex-webhook")

# --- tunables -----------------------------------------------------------

DIM_BRIGHTNESS_PERCENT = 20
RESTORE_BRIGHTNESS_PERCENT = 100

# Tuya data points. Unverified on real hardware (#4): override per light with
# `switch_dp` / `brightness_dp` in rooms.yaml. The cloud API uses fixed codes instead.
TUYA_DEFAULT_SWITCH_DP = 1
TUYA_DEFAULT_BRIGHTNESS_DP = 3
TUYA_CLOUD_SWITCH_CODE = "switch_1"
TUYA_CLOUD_BRIGHTNESS_CODE = "bright_value_v2"
TUYA_RAW_MIN, TUYA_RAW_MAX = 10, 1000


def _read_timeout() -> float:
    try:
        value = float(os.environ.get("LIGHT_READ_TIMEOUT_S", "2"))
    except ValueError:
        return 2.0
    return value if 0 < value <= 30 else 2.0


STATE_READ_TIMEOUT_S = _read_timeout()

GOVEE_LAN_SCAN_PORT = 4001          # multicast scan request target port
GOVEE_LAN_SCAN_MULTICAST_ADDR = "239.255.255.250"
GOVEE_LAN_LISTEN_PORT = 4002        # devices reply here
GOVEE_LAN_CONTROL_PORT = 4003       # unicast control commands go here
GOVEE_LAN_SCAN_TIMEOUT_S = 2.0
# Only RFC 1918 device addresses; is_private also includes special-use ranges.
GOVEE_LAN_PRIVATE_NETWORKS = tuple(IPv4Network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))

GOVEE_CLOUD_API_URL = "https://developer-api.govee.com/v1/devices/control"
GOVEE_CLOUD_STATE_URL = "https://developer-api.govee.com/v1/devices/state"

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


@dataclass(frozen=True)
class StateReading:
    """What a light reported: ``ok`` is False when it could not be read at all."""

    ok: bool
    on: bool | None = None
    brightness: int | None = None  # percent, 1-100

    @staticmethod
    def failed():
        return StateReading(False)


def tuya_raw_to_percent(raw) -> int:
    """Tuya's 10-1000 scale to a 1-100 percent; out-of-range values are clamped."""
    return max(1, min(100, round(raw / 10)))


def tuya_percent_to_raw(percent) -> int:
    return max(TUYA_RAW_MIN, min(TUYA_RAW_MAX, int(percent * 10)))


def brightness_for(action: str):
    """Default brightness percent an action commands; restore has no fixed level (it is read per light)."""
    return DIM_BRIGHTNESS_PERCENT if action == "dim" else None


def _level(action: str, brightness):
    if type(brightness) is int and 1 <= brightness <= 100:
        return brightness
    return DIM_BRIGHTNESS_PERCENT if action == "dim" else RESTORE_BRIGHTNESS_PERCENT


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


def skipped_result(light: dict, reason: str) -> LightResult:
    """A light the dispatcher chose not to touch (off, changed by hand, nothing recorded)."""
    return _fallback_result(_brand_of(light), light, "skipped", reason)


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

    def apply(self, action: str, light: dict, brightness: int | None = None) -> LightResult:
        try:
            result = self._apply(action, light, brightness)
            if isinstance(result, LightResult):
                return result
            return _fallback_result("govee", light, "unconfirmed", "result_unconfirmed")
        except Exception:
            logger.warning(
                "reason=unexpected_controller_error brand=govee operation=apply action=%s id=%s",
                diagnostic_action(action), light.get("id"),
            )
            return _fallback_result("govee", light, "failed", "unexpected_error")

    def _apply(self, action: str, light: dict, brightness: int | None = None) -> LightResult:
        device_id = light.get("id")
        name = light.get("name", device_id)
        brightness = _level(action, brightness)

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

    # --- reading state ---

    def read_state(self, light: dict, timeout: float) -> StateReading:
        """LAN ``devStatus`` first, then the cloud state API. Never raises."""
        for step in (self._read_lan, self._read_cloud):
            try:
                reading = step(light, timeout)
            except Exception:
                logger.info("reason=state_read_failed brand=govee operation=read_state id=%s", light.get("id"))
                continue
            if reading.ok:
                return reading
        return StateReading.failed()

    def _read_lan(self, light: dict, timeout: float) -> StateReading:
        device_id = light.get("id")
        deadline = time.monotonic() + timeout
        ip = _device_secret(device_id).get("ip") or self._discover_ip(device_id, timeout)
        if not ip:
            return StateReading.failed()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", GOVEE_LAN_LISTEN_PORT))
            sock.sendto(json.dumps({"msg": {"cmd": "devStatus", "data": {}}}).encode("utf-8"),
                        (ip, GOVEE_LAN_CONTROL_PORT))
            while (remaining := deadline - time.monotonic()) > 0:
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                if addr[0] != ip:
                    continue
                try:
                    message = json.loads(data.decode("utf-8")).get("msg") or {}
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    continue
                if message.get("cmd") == "devStatus":
                    return self._parse_lan_status(message.get("data"))
        finally:
            sock.close()
        return StateReading.failed()

    @staticmethod
    def _parse_lan_status(data) -> StateReading:
        if not isinstance(data, dict):
            return StateReading.failed()
        on, level = data.get("onOff"), data.get("brightness")
        if on not in (0, 1) or type(level) is not int or not 0 <= level <= 100:
            return StateReading.failed()
        return StateReading(True, bool(on), max(1, level))

    def _read_cloud(self, light: dict, timeout: float) -> StateReading:
        api_key, model = os.environ.get("GOVEE_API_KEY"), light.get("model")
        if not api_key or not model:
            return StateReading.failed()
        import requests

        response = requests.get(GOVEE_CLOUD_STATE_URL, headers={"Govee-API-Key": api_key},
                                params={"device": light.get("id"), "model": model}, timeout=timeout)
        response.raise_for_status()
        properties = {k: v for item in response.json()["data"]["properties"] for k, v in item.items()}
        power, level = properties.get("powerState"), properties.get("brightness")
        if power not in ("on", "off") or type(level) is not int or not 0 <= level <= 100:
            return StateReading.failed()
        return StateReading(True, power == "on", max(1, level))

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

    def _discover_ip(self, device_id: str, timeout: float = GOVEE_LAN_SCAN_TIMEOUT_S) -> str | None:
        """Best-effort UDP multicast discovery. Returns None on any failure."""
        if not device_id:
            return None

        request = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode("utf-8")

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", GOVEE_LAN_LISTEN_PORT))
            sock.settimeout(timeout)
            sock.sendto(request, (GOVEE_LAN_SCAN_MULTICAST_ADDR, GOVEE_LAN_SCAN_PORT))

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                try:
                    reply = json.loads(data.decode("utf-8"))
                except (ValueError, RecursionError):
                    continue
                ip = self._discovery_address(reply, device_id, addr[0])
                if ip is not None:
                    return ip
        except OSError:
            logger.info("reason=discovery_failed brand=govee operation=lan_discovery id=%s fallback=cloud", device_id)
            return None
        finally:
            if sock is not None:
                sock.close()

        return None

    @staticmethod
    def _discovery_address(reply, device_id: str, source_ip: str) -> str | None:
        """Accept only a matching device with a source-matched RFC 1918 IPv4 literal."""
        if not isinstance(reply, dict):
            return None
        message = reply.get("msg")
        if not isinstance(message, dict):
            return None
        payload = message.get("data")
        if not isinstance(payload, dict) or payload.get("device") != device_id:
            return None
        ip = payload.get("ip")
        if not isinstance(ip, str) or ip != source_ip:
            return None
        try:
            address = IPv4Address(ip)
        except ValueError:
            return None
        if not any(address in network for network in GOVEE_LAN_PRIVATE_NETWORKS):
            return None
        return ip

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

    def apply(self, action: str, light: dict, brightness: int | None = None) -> LightResult:
        try:
            result = self._apply(action, light, brightness)
            if isinstance(result, LightResult):
                return result
            return _fallback_result("tuya", light, "unconfirmed", "result_unconfirmed")
        except Exception:
            logger.warning(
                "reason=unexpected_controller_error brand=tuya operation=apply action=%s id=%s",
                diagnostic_action(action), light.get("id"),
            )
            return _fallback_result("tuya", light, "failed", "unexpected_error")

    def _apply(self, action: str, light: dict, brightness: int | None = None) -> LightResult:
        device_id = light.get("id")
        name = light.get("name", device_id)
        brightness = _level(action, brightness)

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

    @staticmethod
    def _dps(light: dict) -> tuple:
        return (light.get("switch_dp", TUYA_DEFAULT_SWITCH_DP), light.get("brightness_dp", TUYA_DEFAULT_BRIGHTNESS_DP))

    # --- reading state ---

    def read_state(self, light: dict, timeout: float) -> StateReading:
        """Local ``status()`` first, then the Tuya cloud status API. Never raises."""
        for step in (self._read_local, self._read_cloud):
            try:
                reading = step(light, timeout)
            except Exception:
                logger.info("reason=state_read_failed brand=tuya operation=read_state id=%s", light.get("id"))
                continue
            if reading.ok:
                return reading
        return StateReading.failed()

    def _read_local(self, light: dict, timeout: float) -> StateReading:
        secret = _device_secret(light.get("id"))
        if not secret.get("local_key") or not secret.get("ip"):
            return StateReading.failed()
        import tinytuya

        device = tinytuya.OutletDevice(light.get("id"), secret["ip"], secret["local_key"])
        device.set_version(3.3)
        device.set_socketTimeout(timeout)
        response = device.status()
        switch_dp, brightness_dp = self._dps(light)
        dps = response.get("dps") if isinstance(response, dict) and not response.get("Error") else None
        if not isinstance(dps, dict):
            return StateReading.failed()
        return self._reading(dps.get(str(switch_dp)), dps.get(str(brightness_dp)))

    def _read_cloud(self, light: dict, timeout: float) -> StateReading:
        access_id, access_key = os.environ.get("TUYA_ACCESS_ID"), os.environ.get("TUYA_ACCESS_KEY")
        if not access_id or not access_key:
            return StateReading.failed()
        import tinytuya

        cloud = tinytuya.Cloud(apiRegion="us", apiKey=access_id, apiSecret=access_key)
        response = cloud.getstatus(light.get("id"))
        if not isinstance(response, dict) or response.get("success") is False:
            return StateReading.failed()
        values = {item.get("code"): item.get("value") for item in response["result"]}
        return self._reading(values.get(TUYA_CLOUD_SWITCH_CODE), values.get(TUYA_CLOUD_BRIGHTNESS_CODE))

    @staticmethod
    def _reading(switch, raw) -> StateReading:
        if type(switch) is not bool:
            return StateReading.failed()
        if not switch:
            return StateReading(True, False, None)
        if type(raw) is not int:
            return StateReading.failed()  # on, but no readable level
        return StateReading(True, True, tuya_raw_to_percent(raw))

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
            # DP 1 is the conventional switch and DP 3 the brightness dp for Tuya dimmers/bulbs;
            # scale is 10-1000 on most firmware. Both are unverified (#4), hence per-light overrides.
            switch_dp, brightness_dp = self._dps(light)
            turn_on = device.turn_on if switch_dp == TUYA_DEFAULT_SWITCH_DP else (lambda: device.set_value(switch_dp, True))
            return self._commands(turn_on,
                                  lambda: device.set_value(brightness_dp, tuya_percent_to_raw(brightness)),
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
                lambda: cloud.sendcommand(device_id, [{"code": TUYA_CLOUD_SWITCH_CODE, "value": True}]),
                lambda: cloud.sendcommand(device_id, [{"code": TUYA_CLOUD_BRIGHTNESS_CODE,
                                                       "value": tuya_percent_to_raw(brightness)}]),
                "cloud", "environment")
        except Exception:
            logger.warning("reason=cloud_control_failed brand=tuya operation=cloud_control id=%s", device_id)
            return Attempt("cloud", "failed", "request_failed", "environment")


# --- dispatch table ---------------------------------------------------------


class NullController:
    """Fallback for an unknown/misconfigured brand - logs and does nothing."""

    def read_state(self, light: dict, timeout: float) -> StateReading:
        return StateReading.failed()

    def apply(self, action: str, light: dict, brightness: int | None = None) -> LightResult:
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


def read_state(light: dict, timeout: float | None = None) -> StateReading:
    """Current on/off and brightness percent of one light; any failure is ``StateReading.failed()``."""
    try:
        reading = get_controller(light.get("brand")).read_state(light, timeout or STATE_READ_TIMEOUT_S)
    except Exception:
        return StateReading.failed()
    return reading if isinstance(reading, StateReading) else StateReading.failed()


def apply_action(action: str, lights: list, brightness=None) -> list:
    """Apply to every light in order; one failure never stops the rest. Returns LightResults.

    ``brightness`` is a percent for all lights, or a dict of light id -> percent.
    """
    results = []
    for light in lights:
        level = brightness.get(light.get("id")) if isinstance(brightness, dict) else brightness
        try:
            result = get_controller(light.get("brand")).apply(action, light, level)
        except Exception:
            result = None
        if not isinstance(result, LightResult):
            result = _fallback_result(_brand_of(light), light, "failed", "unexpected_error")
        results.append(result)
    return results
