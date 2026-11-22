"""The composition root: one Service owns the store, rooms, playback state, queue and metrics.

``create_app`` (app.main) builds a Service in its lifespan: ``start`` opens and
migrates the database, loads the rooms file, seeds the counters and records
``service.config_load``; ``stop`` closes the database. Building a Service opens
nothing, so importing the app has no side effects.
"""
import logging
import threading
import time
from datetime import datetime

from app import audit, auth, lights, storage
from app.action_queue import ActionQueue
from app.db import RestoreRecords, labeled_event_counts, last_received_at
from app.dispatcher import AuditContext, PlaybackState
from app.metrics import Metrics
from app.rooms import RoomRegistry

logger = logging.getLogger("plex-webhook")

# Event names Plex documents for webhooks. Payloads are unauthenticated, so every
# events_total label comes from a fixed or configured set; anything else is "other".
PLEX_EVENTS = frozenset({
    "media.play", "media.pause", "media.resume", "media.stop", "media.scrobble", "media.rate",
    "library.on.deck", "library.new", "admin.database.backup", "admin.database.corrupted",
    "device.new", "playback.started",
})

# How long a graceful stop may spend on queued light actions in all, under Docker's default
# 10 s stop timeout; the last SHUTDOWN_RECORD_SECONDS of it are kept for the terminal records
# of the actions it drops (each audit write waits at most 0.25 s for the database lock).
SHUTDOWN_DRAIN_SECONDS = 5.0
SHUTDOWN_RECORD_SECONDS = 1.0

# Denied-request audit rows are capped per window so a flood of bad calls cannot
# grow the trail unbounded; every denial is still counted in the metric.
DENIAL_AUDIT_LIMIT, DENIAL_AUDIT_WINDOW = 20, 60.0


class Service:
    def __init__(self, settings):
        self.settings = settings
        self.metrics = Metrics()
        self.records = RestoreRecords(settings.db_path)
        self.queue = ActionQueue(settings.dispatch_queue_limit)
        self.conn = None
        self.registry = None
        self.playback = None
        self._denials = {"start": 0.0, "count": 0}
        self._denial_lock = threading.Lock()

    def start(self):
        """Open the store, load the rooms file and seed the counters; see the module docstring.

        A failure after the store opened stops the service (closing the store) and re-raises.
        """
        try:
            self._start()
        except BaseException:
            self.stop()
            raise
        return self

    def _start(self):
        settings = self.settings
        if settings.webhook_token_digest is None:
            logger.warning("reason=webhook_token_unset: POST /webhook accepts any caller that can reach the "
                           "service; set WEBHOOK_TOKEN (see README, Route permissions)")
        lights.configure(settings.devices_secrets_path, settings.light_read_timeout_s)
        self.registry = RoomRegistry(settings.rooms_config_path)
        self.conn = storage.open_database(settings.db_path)
        self.playback = PlaybackState(self.registry, self.metrics, self.queue, self.records)
        self.playback.load_pending_rooms()
        self.playback.init_room_gauges()
        registry = self.registry
        self.record_audit(action="service.config_load", outcome=registry.load_outcome,
                          reason_code=registry.load_reason, correlation=audit.correlation_id(),
                          changed_fields=audit.configuration_changes({}, registry.rooms)
                          if registry.load_outcome == "activated" else {})
        self._seed_counters()

    def stop(self):
        """Stop intake, give queued light actions SHUTDOWN_DRAIN_SECONDS to finish, then close the store.

        Actions still queued after the wait are dropped and audited as ``skipped/shutdown`` while
        the deadline lasts; any left after it are logged by count (``shutdown_drops_unaudited``).
        """
        if not self.queue.close(SHUTDOWN_DRAIN_SECONDS, SHUTDOWN_RECORD_SECONDS):
            logger.warning("reason=shutdown_drain_timeout seconds=%s", SHUTDOWN_DRAIN_SECONDS)
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _seed_counters(self):
        # Seed from SQLite so a restart doesn't report 0 (i.e. 1970) or look like history was wiped.
        last = last_received_at(self.conn)
        if last:
            self.metrics.last_event_timestamp.set(datetime.fromisoformat(last).timestamp())
        for labels, count in labeled_event_counts(self.conn, self.event_labels).items():
            self.metrics.events.labels(*labels).inc(count)

    def event_labels(self, event, player, uuid, account):
        """(event, player, account) label values; inputs use "unknown" for a missing value.

        Players map to the room dispatch resolves them to (by uuid, then title), else
        "other", or "unknown" when no title was sent; accounts are "known" when a title was sent.
        """
        room = self.registry.player_label(None if player == "unknown" else player,
                                          None if uuid == "unknown" else uuid)
        return (
            event if event == "unknown" or event in PLEX_EVENTS else "other",
            room or ("unknown" if player == "unknown" else "other"),
            "other" if account == "unknown" else "known",
        )

    # --- audit ---------------------------------------------------------------

    def audit_db_path(self):
        return self.conn.execute("PRAGMA database_list").fetchone()[2]

    def audit_failed(self):
        logger.warning("reason=audit_write_failed")
        self.metrics.audit_failures.inc()

    def record_audit(self, **fields):
        try:
            audit.append(self.audit_db_path(), **fields)
        except Exception:
            # Fail open. Never expose exception strings or retry webhook/light work.
            self.audit_failed()

    def light_audit_context(self, correlation, event_id, actor="plex_server"):
        """Bind the receipt to the worker. The path is resolved here; the worker opens its own connection."""
        try:
            path = self.audit_db_path()
        except Exception:
            self.audit_failed()
            return None

        def record(**fields):
            try:
                audit.append(path, **fields)
            except Exception:
                self.audit_failed()

        return AuditContext(record, correlation, event_id, actor)

    def denial_audit_allowed(self):
        now = time.monotonic()
        with self._denial_lock:
            if now - self._denials["start"] >= DENIAL_AUDIT_WINDOW:
                self._denials.update(start=now, count=0)
            self._denials["count"] += 1
            return self._denials["count"] <= DENIAL_AUDIT_LIMIT

    def admin_denial(self, authorization, action):
        """None for a valid admin credential; else counts, logs and audits the denial and returns its reason."""
        reason = auth.denial_reason(authorization)
        if reason is None:
            return None
        self.metrics.admin_denials.labels(reason=reason).inc()
        logger.warning("reason=admin_denied code=%s", reason)
        if self.denial_audit_allowed():
            self.record_audit(action=action, outcome="denied", reason_code=reason, correlation=audit.correlation_id())
        return reason

    def expire_idle_players(self):
        """One expiry sweep; each restored room gets its own audit context."""
        self.playback.expire_idle_clients(
            lambda: self.light_audit_context(audit.correlation_id(), None, "playback_expiry"))
