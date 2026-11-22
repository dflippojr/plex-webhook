"""HTTP surface of plex-webhook. ``create_app`` builds the app; the Service (app.service) is built in its lifespan.

``app = create_app()`` stays here for ``uvicorn app.main:app``. Importing this module
reads the environment (app.settings) and opens no file.
"""
import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from prometheus_client import CONTENT_TYPE_LATEST

from app import audit, auth, dispatcher
from app.db import STORED_FIELDS, insert_event, list_known_clients, stored_values
from app.rooms import RoomConfigError, RoomConfigUnavailable
from app.service import Service
from app.settings import Settings

# How often stale plays are checked; see PlaybackState.expire_idle_clients.
EXPIRY_SWEEP_SECONDS = 30
# How long GET /rooms/{room}/lights waits for its queued reads.
LIGHT_CHECK_TIMEOUT_S = 30

# Request bounds for the unauthenticated webhook, sized for a Plex delivery:
# one JSON `payload` field plus an optional thumbnail. The body cap is Settings.webhook_max_body_bytes.
MAX_FORM_FILES, MAX_FORM_FIELDS = 2, 10
MAX_FIELD_BYTES = 512 * 1024


class _BodyTooLarge(Exception):
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("plex-webhook")

# The access log records each request path; anything after /webhook can carry the token.
_WEBHOOK_SUFFIX = re.compile(r"(/webhook)([/?])[^\s\"]+")


def _redact(value):
    return _WEBHOOK_SUFFIX.sub(r"\1\2[redacted]", value) if isinstance(value, str) else value


def _redact_webhook_paths(record):
    """Logging filter: replace what follows /webhook in an access-log record with a placeholder."""
    record.msg = _redact(record.msg)
    if isinstance(record.args, tuple):
        record.args = tuple(_redact(arg) for arg in record.args)
    elif isinstance(record.args, dict):
        record.args = {key: _redact(arg) for key, arg in record.args.items()}
    return True


logging.getLogger("uvicorn.access").addFilter(_redact_webhook_paths)

router = APIRouter()


def create_app(settings=None):
    """The FastAPI app. Settings default to the environment; the Service is built when the app starts."""
    settings = Settings.from_env() if settings is None else settings

    @asynccontextmanager
    async def lifespan(app):
        service = Service(settings).start()
        app.state.service = service
        sweeper = asyncio.create_task(expire_idle_players(service))
        try:
            yield
        finally:
            sweeper.cancel()
            service.stop()

    app = FastAPI(title="plex-webhook", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    return app


def _service(request) -> Service:
    return request.app.state.service


def _require_admin(request, action):
    """Raise 401/503 unless the request carries the configured admin token."""
    reason = _service(request).admin_denial(request.headers.get("authorization"), action)
    if reason is None:
        return
    if reason == "auth_unconfigured":
        raise HTTPException(status_code=503, detail="Admin authentication unavailable")
    raise HTTPException(status_code=401, detail="Admin credential required",
                        headers={"WWW-Authenticate": "Bearer"})


async def expire_idle_players(service):
    while True:
        await asyncio.sleep(EXPIRY_SWEEP_SECONDS)
        try:
            service.expire_idle_players()
        except Exception:
            logger.error("reason=dispatcher_action_failed operation=expire")


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/metrics")
async def metrics(request: Request):
    return Response(_service(request).metrics.render(), media_type=CONTENT_TYPE_LATEST)


@router.get("/clients")
async def clients(request: Request):
    """Distinct Plex clients seen so far, to help fill in the local config/rooms.yaml."""
    _require_admin(request, "clients.read")
    return {"clients": list_known_clients(_service(request).conn)}


@router.get("/rooms")
async def rooms(request: Request):
    _require_admin(request, "rooms.read")
    return {"rooms": _service(request).registry.rooms}


@router.get("/rooms/{room}/lights")
async def check_room_lights(room: str, request: Request):
    """Read every configured light of a room on the light worker. Never changes a light."""
    _require_admin(request, "lights.check")
    service = _service(request)
    correlation = audit.correlation_id()
    if room not in service.registry.rooms:
        # The key is request text, so it stays out of the trail.
        service.record_audit(action="lights.check", outcome="rejected", reason_code="unknown_room",
                      correlation=correlation, admin=True)
        raise HTTPException(status_code=404, detail="Unknown room")
    try:
        entries = await asyncio.wait_for(asyncio.wrap_future(service.playback.check_lights(room)), LIGHT_CHECK_TIMEOUT_S)
        outcome, reason = "completed", "read"
    except asyncio.TimeoutError:
        outcome, reason = "failed", "timed_out"
    except dispatcher.CheckDropped as exc:
        outcome, reason = "failed", exc.reason
    except Exception:
        logger.error("reason=light_check_failed")
        outcome, reason = "failed", "check_error"
    service.record_audit(action="lights.check", outcome=outcome, reason_code=reason, correlation=correlation,
                  admin=True, target_id=room)
    if outcome != "completed":
        status, detail = LIGHT_CHECK_FAILURES[reason]
        raise HTTPException(status_code=status, detail=detail)
    return {"room": room, "lights": entries}


LIGHT_CHECK_FAILURES = {
    "timed_out": (504, "Light check timed out"),
    "queue_overflow": (503, "Light check queue full"),
    "superseded": (503, "Light check superseded"),
    "shutdown": (503, "Light check stopped by shutdown"),
    "check_error": (500, "Light check failed"),
}


@router.post("/rooms/reload")
async def reload_rooms(request: Request):
    _require_admin(request, "rooms.reload")
    service = _service(request)
    registry = service.registry
    before = registry.rooms
    correlation = audit.correlation_id()
    _room_config_operation(service, registry.reload, "rooms.reload", correlation)
    service.record_audit(action="rooms.reload", outcome="activated", reason_code="loaded", correlation=correlation,
                         changed_fields=audit.configuration_changes(before, registry.rooms), admin=True)
    service.playback.init_room_gauges()
    return {"status": "reloaded", "rooms": list(registry.rooms.keys())}


def _room_config_operation(service, operation, action, correlation):
    try:
        return operation()
    except RoomConfigError as exc:
        service.record_audit(action=action, outcome="rejected", reason_code="invalid_config", correlation=correlation,
                             admin=True)
        raise HTTPException(status_code=422, detail=exc.detail) from None
    except RoomConfigUnavailable:
        service.record_audit(action=action, outcome="rejected", reason_code="config_unavailable",
                             correlation=correlation, admin=True)
        raise HTTPException(status_code=503, detail="Rooms configuration unavailable") from None


@router.post("/rooms/validate")
async def validate_rooms(request: Request):
    _require_admin(request, "rooms.validate")
    service = _service(request)
    correlation = audit.correlation_id()
    candidate, *_ = _room_config_operation(service, service.registry.validate, "rooms.validate", correlation)
    service.record_audit(action="rooms.validate", outcome="validated", reason_code="valid", correlation=correlation, admin=True)
    return {"status": "valid", "rooms": list(candidate)}


@router.post("/webhook")
async def plex_webhook(request: Request):
    service = _service(request)
    correlation = audit.correlation_id()
    if service.settings.webhook_token_digest is not None:
        _reject_webhook_token(service, correlation)
    return await _receive_webhook(service, request, correlation)


@router.post("/webhook/{token:path}")
async def plex_webhook_with_token(token: str, request: Request):
    """The webhook with WEBHOOK_TOKEN as the rest of the path; without a configured token it does not exist.

    The whole remainder, empty or with slashes, is compared, so no path under /webhook/ reaches the
    router's trailing-slash redirect, which would echo the presented value in its Location header.
    """
    service = _service(request)
    digest = service.settings.webhook_token_digest
    if digest is None:
        if not token:
            # Unset token: POST /webhook/ keeps the redirect to /webhook it got before this route existed.
            return RedirectResponse(request.url.replace(path="/webhook"), status_code=307)
        raise HTTPException(status_code=404, detail="Not Found")
    correlation = audit.correlation_id()
    if not auth.webhook_token_matches(token, digest):
        _reject_webhook_token(service, correlation)
    return await _receive_webhook(service, request, correlation)


def _reject_webhook_token(service, correlation):
    """404, as for a route that does not exist, before the body is read. Nothing is stored or dispatched."""
    service.metrics.webhook_rejected.labels(reason="invalid_token").inc()
    logger.warning("reason=webhook_rejected code=invalid_token")
    if service.denial_audit_allowed():
        service.record_audit(action="webhook.receipt", outcome="denied", reason_code="invalid_token",
                             correlation=correlation)
    raise HTTPException(status_code=404, detail="Not Found")


async def _receive_webhook(service, request, correlation):
    settings, metrics = service.settings, service.metrics
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            _reject_invalid_form(service, correlation)
        if int(declared) > settings.webhook_max_body_bytes:
            _reject_oversized(service, "body_too_large")
    _limit_streamed_body(request, settings.webhook_max_body_bytes)
    try:
        form = await request.form(max_files=MAX_FORM_FILES, max_fields=MAX_FORM_FIELDS,
                                  max_part_size=MAX_FIELD_BYTES)
    except _BodyTooLarge:
        _reject_oversized(service, "body_too_large")
    except StarletteHTTPException as exc:
        detail = str(exc.detail)
        if detail.startswith("Too many files"):
            _reject_oversized(service, "too_many_files")
        if detail.startswith("Too many fields"):
            _reject_oversized(service, "too_many_fields")
        if detail.startswith(("Part exceeded", "Field exceeded")):
            _reject_oversized(service, "field_too_large")
        _reject_invalid_form(service, correlation)
    except Exception:
        _reject_invalid_form(service, correlation)

    payload = None
    decoded_payload = None
    reason = "missing_payload"
    payload_raw = form.get("payload")
    if payload_raw is not None:
        try:
            payload = json.loads(payload_raw)
            # Nesting that loads but cannot be serialized again, and NaN/Infinity that would be
            # written back as non-JSON tokens, must not reach the storage writes below.
            json.dumps({"payload": payload}, allow_nan=False)
            decoded_payload = payload
            reason = "accepted" if _valid_payload(payload) else "invalid_payload"
        except (ValueError, TypeError, RecursionError):
            reason = "invalid_json"
            logger.warning("payload field present but not valid JSON")
    if reason != "accepted":
        payload = None
    elif not service.registry.server_allowed(payload):
        # Stored and counted like any event, but never dispatched.
        reason = "server_not_allowed"
        metrics.unlisted_server_events.inc()
        logger.warning("reason=server_not_allowed")

    attachments = [
        {"field": key, "filename": getattr(value, "filename", None), "content_type": getattr(value, "content_type", None)}
        for key, value in form.multi_items()
        if hasattr(value, "filename")
    ]

    event_type = payload.get("event") if payload else None
    received_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")

    raw_json, truncated = _bounded_raw(json.dumps(decoded_payload), settings.raw_payload_max_bytes)
    event_id = insert_event(service.conn, received_at, event_type, payload, raw_json, truncated, attachments)
    service.record_audit(action="webhook.receipt",
                         outcome="received" if reason in ("accepted", "server_not_allowed") else "rejected",
                         reason_code=reason, correlation=correlation, event_id=event_id)

    account_title = ((payload or {}).get("Account") or {}).get("title") or "unknown"
    player = (payload or {}).get("Player") or {}
    player_title, player_uuid = player.get("title") or "unknown", player.get("uuid") or "unknown"
    metrics.events.labels(*service.event_labels(event_type or "unknown", player_title, player_uuid,
                                                account_title)).inc()
    metrics.last_event_timestamp.set(time.time())

    if reason == "accepted":
        service.playback.handle_event(payload, service.light_audit_context(correlation, event_id))

    logger.info("captured event=%s", event_type)
    return {"status": "received", "event": event_type}


def _reject_invalid_form(service, correlation):
    """400 for a body that is not a readable form. The receipt is audited; nothing is stored."""
    service.record_audit(action="webhook.receipt", outcome="rejected", reason_code="invalid_form", correlation=correlation)
    raise HTTPException(status_code=400, detail="Invalid webhook form") from None


def _reject_oversized(service, reason):
    """413 for a request over the bounds. Nothing is stored; the metric counts it."""
    service.metrics.webhook_rejected.labels(reason=reason).inc()
    logger.warning("reason=webhook_rejected code=%s", reason)
    raise HTTPException(status_code=413, detail="Webhook request too large")


def _limit_streamed_body(request, limit):
    """Count bytes as the form parser reads them, so a missing or false Content-Length cannot bypass the cap."""
    receive, seen = request._receive, 0

    async def limited():
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                raise _BodyTooLarge
        return message

    request._receive = limited


def _bounded_raw(text, limit):
    """Cap the stored raw payload at ``limit`` bytes of UTF-8; returns (text, truncated)."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="ignore"), True


def _valid_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        return False
    # Reject shapes that would otherwise raise during raw capture/dispatch.
    for group, names in STORED_FIELDS:
        fields = payload.get(group)
        if fields is not None and not isinstance(fields, dict):
            return False
        allowed = (str, int, float) if group == "Metadata" else str
        if fields and any(fields.get(name) is not None and not isinstance(fields[name], allowed) for name in names):
            return False
    return all(_storable(value) for value in (payload["event"], *stored_values(payload)))


def _storable(value):
    """False for what SQLite or UTF-8 cannot take: a lone surrogate or an integer beyond 64 bits."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
    elif isinstance(value, int):
        return -(2 ** 63) <= value < 2 ** 63
    return True


app = create_app()
