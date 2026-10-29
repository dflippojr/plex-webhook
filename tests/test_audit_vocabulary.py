"""Characterization of the audit vocabulary; any change here changes the trail contract."""
from app import audit, audit_cli, auth

DENIED = ["auth_unconfigured", "invalid_credential", "missing_credential"]
CONFIG_REJECTED = ["config_unavailable", "invalid_config"]
LIGHT_FAILURES = ["library_unavailable", "light_off", "manual_change", "missing_credentials", "missing_model",
                  "no_address", "no_record", "request_failed", "send_error", "tuya_rejected", "unexpected_error",
                  "unsupported_brand"]

OPERATIONS = {
    "clients.read": ("http", "anonymous", "clients", {"denied": DENIED}),
    "light.action_queued": ("dispatcher", "system", "room", {"queued": ["accepted"]}),
    "light.action_summary": ("dispatcher", "system", "room", {
        "completed_unverified": ["all_sent"], "failed": ["all_failed", "dispatcher_error"],
        "partial": ["mixed_results"],
        "skipped": ["all_skipped", "no_lights", "own_playback_active", "owned_by_newer_source", "queue_overflow",
                    "superseded"],
        "unconfirmed": ["all_unconfirmed", "no_results", "some_unconfirmed"],
    }),
    "light.decision": ("dispatcher", "system", "light", {
        "dim_kept_original": ["record_exists"], "dim_recorded": ["at_dim_level", "read_failed", "read_ok"],
        "dim_skipped_off": ["was_off"], "restore_without_read": ["read_failed"], "restored": ["within_tolerance"],
        "skipped_manual_change": ["brightness_changed", "turned_off"], "skipped_was_off": ["was_off"],
    }),
    "light.result": ("dispatcher", "system", "light", {
        "command_sent": ["lan_command_sent"], "failed": LIGHT_FAILURES,
        "request_accepted_by_transport": ["cloud_accepted", "local_accepted"], "skipped": LIGHT_FAILURES,
        "unconfirmed": ["result_unconfirmed"],
    }),
    "lights.check": ("http", "anonymous", "room", {
        "completed": ["read"], "denied": DENIED, "failed": ["check_error", "queue_overflow", "superseded", "timed_out"],
        "rejected": ["unknown_room"],
    }),
    "rooms.read": ("http", "anonymous", "rooms", {"denied": DENIED}),
    "rooms.reload": ("http", "anonymous", "rooms", {
        "activated": ["loaded"], "denied": DENIED, "rejected": CONFIG_REJECTED,
    }),
    "rooms.validate": ("http", "anonymous", "rooms", {
        "denied": DENIED, "rejected": CONFIG_REJECTED, "validated": ["valid"],
    }),
    "service.config_load": ("service", "system", "rooms", {"activated": ["loaded"], "rejected": CONFIG_REJECTED}),
    "webhook.receipt": ("webhook", "plex_server", "webhook", {
        "received": ["accepted", "server_not_allowed"],
        "rejected": ["invalid_form", "invalid_json", "invalid_payload", "missing_payload"],
    }),
}


def test_operations_vocabulary_is_frozen():
    snapshot = {action: (source, actor, target, {outcome: sorted(reasons) for outcome, reasons in outcomes.items()})
                for action, (source, actor, target, outcomes) in audit.OPERATIONS.items()}
    assert snapshot == OPERATIONS
    assert all(isinstance(reasons, set) for *_, outcomes in audit.OPERATIONS.values() for reasons in outcomes.values())


def test_denial_reasons_order_and_cli_columns_are_frozen():
    # main.py pre-creates denial metric labels in this order.
    assert auth.DENIAL_REASONS == ("auth_unconfigured", "missing_credential", "invalid_credential")
    assert audit_cli.COLUMNS == ("id", "recorded_at", "correlation_id", "source", "actor_kind", "actor_id",
                                 "actor_verified", "action", "target_kind", "target_id", "outcome", "reason_code",
                                 "changed_fields", "checksum")
