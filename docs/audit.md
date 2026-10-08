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
  empty mapping. Its actor is the system. Reload/validation callers and webhook
  senders are anonymous and unverified. There is no authentication on these
  endpoints, so a receipt cannot establish that Plex sent it. Plex account/client
  identifiers are payload claims and are omitted from this v1 trail.

Each operation gets a server-generated correlation UUID; client headers cannot
supply it. IDs increase in append order (not necessarily request start order),
and `recorded_at` is UTC at the append attempt. Receipts normally reference the
numeric legacy `events.id`; no raw data is joined during export. IDs and
correlations are private operational data, not public identifiers. There are no
light outcome records yet (#51).

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

Legacy `events` and `events.jsonl` still store full raw payloads and attachment
metadata. They are a separate privacy surface. This change does not prune,
redact or migrate them; coordinate that later with media-tracker-sync.

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
the boundary is retained. Count and delete share a transaction. Legacy tables
and JSONL are untouched. AUTOINCREMENT prevents ID reuse after pruning. Prune
does not vacuum the database; removing rows does not necessarily shrink the file.
The command cannot verify whether another process has the service running.

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
