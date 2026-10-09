"""Owner-only offline audit reader and explicit retention maintenance.

Run: python -m app.audit_cli --db /explicit/path.db list
Only stdlib and app.audit are imported; never the service or its devices.
"""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.audit import OPERATIONS

COLUMNS = ("id", "recorded_at", "correlation_id", "source", "actor_kind", "actor_id",
           "actor_verified", "action", "target_kind", "target_id", "outcome", "reason_code",
           "changed_fields", "checksum")


def open_database(path, *, writable=False):
    uri = Path(path).resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    return conn


def utc_argument(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except ValueError:
        raise argparse.ArgumentTypeError("Use an ISO timestamp with a timezone") from None


def bounded_limit(value):
    number = int(value)
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError("Limit must be between 1 and 10000")
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("ID must be nonnegative")
    return number


def read_records(conn, args):
    clauses, params = ["id > ?"], [args.after_id]
    for column, operator, value in (
        ("id", "=", args.id), ("correlation_id", "=", args.correlation_id),
        ("action", "=", args.action), ("recorded_at", ">=", args.since),
        ("recorded_at", "<", args.until),
    ):
        if value is not None:
            clauses.append(f"{column} {operator} ?")
            params.append(value)
    params.append(args.limit)
    columns = COLUMNS
    if "detail" in {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}:
        columns = COLUMNS + ("detail",)
    # Column names/operators come exclusively from fixed application constants.
    query = f"SELECT {', '.join(columns)} FROM audit_events WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?"
    for row in conn.execute(query, params):
        record = dict(row)
        record["changed_fields"] = json.loads(record["changed_fields"])
        # Only light records carry detail; omitting it elsewhere keeps legacy checksums verifiable.
        if record.pop("detail", None) is not None:
            record["detail"] = json.loads(row["detail"])
        yield record


def prune(conn, *, apply=False, now=None):
    """Sole supported deletion path: explicit offline owner-run 90-day retention."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=90)).isoformat(timespec="microseconds")
    # Lock count+delete together only for the explicitly writable operation.
    if apply:
        conn.execute("BEGIN IMMEDIATE")
    with conn:
        count = conn.execute("SELECT COUNT(*) FROM audit_events WHERE recorded_at < ?", (cutoff,)).fetchone()[0]
        if apply:
            conn.execute("DELETE FROM audit_events WHERE recorded_at < ?", (cutoff,))
    return {"cutoff": cutoff, "eligible": count, "deleted": count if apply else 0, "dry_run": not apply}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--db", required=True, help="Explicit existing SQLite database")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("list", "export"):
        command = commands.add_parser(name)
        command.add_argument("--limit", type=bounded_limit, default=100)
        command.add_argument("--after-id", type=nonnegative, default=0)
        command.add_argument("--id", type=nonnegative)
        command.add_argument("--correlation-id")
        command.add_argument("--action", choices=sorted(OPERATIONS))
        command.add_argument("--since", type=utc_argument)
        command.add_argument("--until", type=utc_argument)
    command = commands.add_parser("prune", help="Offline 90-day retention, dry-run by default")
    command.add_argument("--apply", action="store_true", help="Delete eligible audit rows; service must be stopped")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        with closing(open_database(args.db, writable=args.command == "prune" and args.apply)) as conn:
            if args.command == "prune":
                print(json.dumps(prune(conn, apply=args.apply), sort_keys=True))
            else:
                for record in read_records(conn, args):
                    if args.command == "export":
                        print(json.dumps(record, sort_keys=True))
                    else:
                        print(f"{record['id']} {record['recorded_at']} {record['correlation_id']} "
                              f"{record['action']} {record['outcome']} {record['reason_code']}")
    except (sqlite3.Error, OSError, ValueError):
        # Database paths/content and exception text can contain private data.
        print("audit_cli_failed: existing readable audit database required", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
