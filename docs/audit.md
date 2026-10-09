# Owner audit review

The existing SQLite database gains `audit_events` on the first audit append
(normally service startup). Existing events, counters and indexes are preserved;
there is no backfill from raw payloads. No audit HTTP endpoint exists. Review and
export are available only to the filesystem-authorized owner through the offline
CLI. Protect the database, backups and exports with owner-only filesystem ACLs.

## What the records mean

- `webhook.receipt`: `received/accepted` means the decoded payload has a usable
  shape. `rejected` means missing payload, invalid JSON/shape or an unreadable
  form. Missing/invalid payloads retain the existing HTTP 200/raw-history
  behavior; a malformed form returns sanitized HTTP 400. A rejected receipt
  never claims that an action happened. Receipt is not dispatch or light success.
- `rooms.reload`: `activated/loaded` means validated state was published;
  `rejected/invalid_config` or `rejected/config_unavailable` leaves the old
  mapping and playback state unchanged. A successful no-op reload is still an
  activation observation, with empty changes.
- `rooms.validate`: `validated/valid` or a rejected validation; never records
  configuration changes or activates state.
- `service.config_load`: the startup load result, including failure with an
  empty mapping. Its actor is the system. Reload/validation by a valid admin token
  records actor `admin_token`/`owner-admin`, verified (a shared client
  credential, not a person). Webhook receipts record `plex_server`/`plex-server`,
  unverified: the webhook is unauthenticated, so a receipt cannot establish that
  Plex sent it. Denied calls are `outcome=denied` with reason `missing_credential`,
  `invalid_credential` or `auth_unconfigured`, an anonymous actor, on
  `rooms.reload`, `rooms.validate`, `rooms.read` and `clients.read`; at most 20
  such rows per minute are written (all are counted in a metric). Plex account/client
  identifiers are payload claims and are omitted from this v1 trail.

Each operation gets a server-generated correlation UUID; client headers cannot
supply it. IDs increase in append order (not necessarily request start order),
and `recorded_at` is UTC at the append attempt. Receipts normally reference the
numeric legacy `events.id`; no raw data is joined during export. IDs and
correlations are private operational data, not public identifiers. Light outcomes
are recorded as described under Light actions below.

Changes summarize room additions/removals (`rooms`), the absolute room count
delta (`rooms.count`), and the number of rooms whose clients, lights or other
fields changed (`rooms.plex_clients`, `rooms.lights`, `rooms.other`). These
fixed group paths/counts never include room names, field values, YAML source,
filenames, headers, IP addresses, media titles, tokens or exception strings.
`rooms.other` covers names and extension fields without exposing their keys.

## Read and export

Python 3.12 with the repository available is sufficient; this CLI uses only the
standard library and `app.audit`. It does not import/start the web service,
dispatcher, config loader or light controllers. Supply the database explicitly:

```powershell
python -m app.audit_cli --db 'D:/Docker/plex-webhook/data/plex_events.db' list --limit 100
python -m app.audit_cli --db 'D:/Docker/plex-webhook/data/plex_events.db' export --action rooms.reload --since '2026-10-08T00:00:00Z' --until '2026-10-09T00:00:00Z' --limit 1000
```

`list` prints compact rows; `export` prints JSONL with every audit field,
including the checksum and change counts. Both use SQLite `mode=ro` and
`query_only`; a missing database is never created. Defaults are 100 rows,
maximum 10,000 per invocation, ascending ID order. `--id`, `--correlation-id`,
`--action`, inclusive `--since` and exclusive `--until` may be combined.
Timestamps require a timezone and normalize to UTC. Paginate using
`--after-id <last-returned-id>`; newly appended rows may appear in later pages.
Use a consistent backup for a fixed export snapshot. Keep redirected exports
private (PowerShell 7 UTF-8 redirection is recommended).

## Light actions

A room transition (first client plays = `dim`, last client stops = `restore`) writes, all
sharing the **receipt's `correlation_id`** and one `detail.action_id`:

1. `light.action_queued` (`queued/accepted`, target = room key): written when the work is
   queued, with a snapshot of the resolved lights and brightness levels. A config reload
   afterwards does not change a queued action.
2. `light.decision` per light that has a brightness decision (target = configured light
   ID), before its result: outcome `restored`, `skipped_manual_change` (`brightness_changed`
   or `turned_off`), `skipped_was_off` or `restore_without_read` on restore, and
   `dim_recorded`, `dim_skipped_off` or `dim_kept_original` on dim. `detail` carries
   `dim_percent`, `restore_percent` and `observed_percent` (null when unknown). The queued
   record's `brightness` is the dim level, or null for a restore (read per light at run time).
3. `light.result` per light (target = configured light ID), in order. A failing light never
   stops later lights or actions.
4. `light.action_summary` (target = room key): the terminal record. Without it the action
   is **incomplete** (for example after abrupt termination); commands are never replayed on
   restart and success is never synthesized.

An action dropped before it started has only the queued record and a `skipped` summary
with no decisions or results: `superseded` when a newer action for the same room replaced
it, `queue_overflow` when the bounded queue pushed it out. An automation that yields to its
target room's own playback is summarized as `skipped/own_playback_active`. Queued automation
batches write nothing until they run, so a dropped batch leaves no record.

`detail.on_behalf_of` is the initiating actor (the unverified `plex-server`). The row's own
actor is `system`. `export` adds a `detail` object to these rows only.

Outcomes are transport evidence, **never observed bulb state**:

| outcome | meaning |
| --- | --- |
| `command_sent` | Govee LAN UDP datagrams were sent; there is no acknowledgement |
| `request_accepted_by_transport` | cloud HTTP completed, or the local/cloud Tuya reply held no error |
| `unconfirmed` | a call returned but nothing establishes acceptance (no fallback is attempted) |
| `failed` | every attempt errored or was explicitly rejected (`tuya_rejected`) |
| `skipped` | nothing was sent: `light_off`, `manual_change` or `no_record` (decided not to touch the light), or nothing could be tried (`unsupported_brand`, `missing_credentials`, `missing_model`, `no_address`, `library_unavailable`) |

`detail` also holds the selected `transport` (`lan`, `local`, `cloud`, `none`), the
`credential_source` kind (`environment`, `device_config`, `none`, never a value),
`progress` (`turn`, `brightness` completed steps) and every `attempts` entry, so a local
failure followed by a cloud fallback is visible. Summary outcomes: `completed_unverified`,
`partial`, `unconfirmed`, `failed`, `skipped`, with per-outcome `counts`.

Follow one action: `list --correlation-id <id>` or `export --correlation-id <id>`.
Audit write failures after a command was sent are counted and logged, never retried.

`plex_dispatcher_actions_total` is unchanged: it counts completed dispatcher calls, not
verified per-device success.

## Failure and integrity limits

Audit failures **do not block webhooks, validation or reload**. The app logs
only `reason=audit_write_failed` without exception text and increments
`plex_webhook_audit_write_failures_total`. The counter reports lost append
attempts since process start and resets on restart. Alert on its increase; the
durable trail will have a gap. There is no replay queue, side-effect retry or
fabricated completion. Audit uses an independent connection per append with a
250 ms lock timeout. A lock can cause a lost audit record even though activation
succeeds. This policy does not make legacy JSONL/SQLite writes fail-open, and
those files are not an atomic transaction with audit.

Application code only appends records. The sole supported deletion path is
explicit retention maintenance below. SQLite file owners can edit/delete records
and recompute checksums. The unkeyed SHA-256 checksum detects accidental record
changes cheaply; it is not a signed chain, does not detect removed rows and is
not tamper-proof. `app.audit.checksum(exported_record)` recomputes it (excluding
`id` and `checksum`, with decoded `changed_fields`). Host file edits, credential
rotation, deployments and restore commands are outside application visibility;
startup/reload records observe configuration and never name its editor.

Legacy `events` and `events.jsonl` store raw payloads (capped, see README "Request
and storage bounds") and attachment metadata. They are a separate privacy
surface; they are pruned by the same retention command below but are not redacted.
Coordinate redaction with media-tracker-sync.

## Retention maintenance (owner only)

Retention is 90 days. There is **no automatic deletion/job**. Review eligibility
with a read-only dry run, then stop the service before running the explicit
writable prune. Arrange the shutdown/restart yourself in a maintenance window.

```powershell
python -m app.audit_cli --db 'D:/Docker/plex-webhook/data/plex_events.db' prune
# After the service is stopped and a consistent backup is saved:
python -m app.audit_cli --db 'D:/Docker/plex-webhook/data/plex_events.db' prune --apply
```

Only rows with `recorded_at` strictly older than now minus 90 days are deleted;
the boundary is retained. Count and delete share a transaction.

The same command also covers stored events: `events` rows with `received_at`
older than the cutoff, and the matching lines of `events.jsonl`. The log defaults
to `events.jsonl` beside the database (skipped if absent); override with
`--events-log PATH`. Output reports `events_eligible`/`events_deleted` and
`event_log_eligible`/`event_log_removed` next to the audit counts. Dry run (the
default) changes nothing. With `--apply`, the database rows are deleted in one
transaction, then `events.jsonl` is rewritten to a temp file and swapped in; lines
that are not parseable or lack a timezone-aware `received_at` are kept. Stop the
service first: it appends to the log while running. AUTOINCREMENT prevents ID reuse
after pruning. Prune does not vacuum the database; removing rows does not
necessarily shrink the file. The command cannot verify whether another process has
the service running.

## Consistent backup and restore

Use SQLite's backup API, not a live file copy; it includes committed SQLite
state consistently even with journal/WAL sidecars. Save a new filename with
owner-only permissions. From the repository, this owner-run command backs up the
whole database (legacy and audit); it refuses an already existing destination:

```powershell
python -c "import sqlite3; from pathlib import Path; source=Path('D:/Docker/plex-webhook/data/plex_events.db'); dest=Path('D:/Backups/plex-events-2026-10-08.db'); assert not dest.exists(), 'Choose a new backup filename'; src=sqlite3.connect(source.as_uri()+'?mode=ro', uri=True); dst=sqlite3.connect(dest); src.backup(dst); dst.close(); src.close()"
```

Create `D:/Backups` first with appropriate permissions. A fixture test exercises
this same SQLite backup/restore API and compares the saved audit rows/checksums.
For restore, stop the service, preserve the current database and any sidecars
together as a rollback set, then restore via the backup API to a **new** file:

```powershell
python -c "import sqlite3; from pathlib import Path; source=Path('D:/Backups/plex-events-2026-10-08.db'); dest=Path('D:/Docker/plex-webhook/data/restored.db'); assert not dest.exists(), 'Choose a new restore filename'; src=sqlite3.connect(source.as_uri()+'?mode=ro', uri=True); dst=sqlite3.connect(dest); src.backup(dst); dst.close(); src.close()"
python -m app.audit_cli --db 'D:/Docker/plex-webhook/data/restored.db' list --limit 10
```

Verify `PRAGMA integrity_check` on the restored fixture, then, with the service
still stopped and the old database/sidecars moved aside, rename `restored.db` to
`plex_events.db`, restore its writable permissions and restart the service.
Never combine restored files with old `-wal`, `-shm` or `-journal` sidecars.
Restoring an older backup loses all newer audit and legacy SQLite rows; there is
no separate journal surviving restore. The independent JSONL may then disagree
with SQLite. Startup after restore records a new load observation, not proof of
who restored the file.
