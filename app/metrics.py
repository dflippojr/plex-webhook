"""The service's Prometheus collectors, bound to one registry per service.

Names, help text, labels and registration order are part of the /metrics contract
a dashboard reads: tests/test_metrics_golden.py compares the output with master's.
The process, platform and GC collectors are registered first, as on the default
registry, so /metrics keeps their families.
"""
from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

from app import auth

WEBHOOK_REJECTION_REASONS = ("body_too_large", "too_many_fields", "too_many_files", "field_too_large",
                             "invalid_token")


class Metrics:
    def __init__(self):
        self.registry = registry = CollectorRegistry(auto_describe=True)
        GCCollector(registry=registry)
        PlatformCollector(registry=registry)
        ProcessCollector(registry=registry)

        self.room_active_sessions = Gauge(
            "plex_dispatcher_room_active_sessions", "Number of Plex clients currently playing in a room",
            ["room"], registry=registry)
        self.dispatch_actions = Counter(
            "plex_dispatcher_actions_total", "Total light actions dispatched", ["room", "action"], registry=registry)
        self.expired_players = Counter(
            "plex_dispatcher_expired_players_total",
            "Players dropped from a room's active state after a play with no matching pause/stop went stale",
            ["room"], registry=registry)
        self.light_decisions = Counter(
            "plex_dispatcher_light_decisions_total", "Per-light brightness decisions taken when dimming and restoring",
            ["room", "decision"], registry=registry)
        self.dispatch_dropped = Counter(
            "plex_dispatcher_actions_dropped_total", "Queued light actions dropped before they started",
            ["room", "reason"], registry=registry)
        self.light_results = Counter(
            "plex_dispatcher_light_results_total",
            "Per-light results by transport and outcome: transport evidence, never observed bulb state",
            ["room", "brand", "transport", "outcome"], registry=registry)

        self.events = Counter(
            "plex_webhook_events_total", "Total Plex webhook events received", ["event", "player", "account"],
            registry=registry)
        self.last_event_timestamp = Gauge(
            "plex_webhook_last_event_timestamp_seconds", "Unix timestamp of the last received Plex webhook event",
            registry=registry)
        self.audit_failures = Counter(
            "plex_webhook_audit_write_failures_total", "Audit records lost to write failures", registry=registry)
        self.webhook_rejected = Counter(
            "plex_webhook_rejected_requests_total", "Webhook requests rejected before storage", ["reason"],
            registry=registry)
        for reason in WEBHOOK_REJECTION_REASONS:
            self.webhook_rejected.labels(reason=reason)
        self.unlisted_server_events = Counter(
            "plex_webhook_unlisted_server_events_total",
            "Webhook events stored but not dispatched: Server.uuid not allowed", registry=registry)
        self.admin_denials = Counter(
            "plex_webhook_admin_denials_total", "Denied admin requests", ["reason"], registry=registry)
        for reason in auth.DENIAL_REASONS:
            self.admin_denials.labels(reason=reason)

    def render(self):
        return generate_latest(self.registry)
