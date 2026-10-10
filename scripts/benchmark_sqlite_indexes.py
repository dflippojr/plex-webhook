"""Offline synthetic benchmark: python -m scripts.benchmark_sqlite_indexes.

Only imports app.db definitions. All databases are created under system temp;
there is deliberately no argument for an existing database path.
"""

import argparse
import json
import platform
import shutil
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from app import db

INDEX_NAMES = ("idx_events_labels_cover", "idx_events_clients_cover")
QUERIES = {"event_counts": db.event_counts, "list_known_clients": db.list_known_clients}


def drop_new_indexes(conn):
    for name in INDEX_NAMES:
        conn.execute(f"DROP INDEX {name}")
    conn.commit()


def synthetic_rows(count):
    events = ("media.play", "media.pause", "media.resume", "media.stop")
    for i in range(count):
        yield (
            f"2026-10-01T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00",
            events[i % 4],
            f"TV{i % 12}",
            f"uuid{i % 12}",
            f"user{(i // 12) % 3}",
            "x" * 1024,
        )


def seed_history(conn, count):
    conn.executemany(
        "INSERT INTO events "
        "(received_at,event,player_title,player_uuid,account_title,raw_payload) "
        "VALUES (?,?,?,?,?,?)",
        synthetic_rows(count),
    )
    conn.commit()


def query_plan(conn, function):
    """Explain the actual application SELECT, without duplicating its SQL."""
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        function(conn)
    finally:
        conn.set_trace_callback(None)
    return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + statements[0])]


def read_trials(conn, function):
    rows = function(conn)  # One warmup, followed by seven measured trials.
    samples = []
    for _ in range(7):
        start = time.perf_counter()
        assert function(conn) == rows
        samples.append((time.perf_counter() - start) * 1000)
    return rows, {
        "median_ms": statistics.median(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "samples_ms": samples,
        "plan": query_plan(conn, function),
    }


def write_trials(conn, count):
    samples = []
    for received_at, event, title, uuid, account, raw in synthetic_rows(count):
        payload = {"Player": {"title": title, "uuid": uuid}, "Account": {"title": account}}
        start = time.perf_counter()
        db.insert_event(conn, received_at, event, payload, raw)
        samples.append((time.perf_counter() - start) * 1000)
    return {
        "commits": count,
        "median_ms": statistics.median(samples),
        "p95_ms": statistics.quantiles(samples, n=100, method="inclusive")[94],
    }


def benchmark(scratch, count, writes):
    baseline_path = scratch / f"baseline-{count}.db"
    indexed_path = scratch / f"indexed-{count}.db"
    with sqlite3.connect(baseline_path) as conn:
        conn.executescript(db.SCHEMA)
        drop_new_indexes(conn)
        seed_history(conn, count)
    conn.close()
    shutil.copyfile(baseline_path, indexed_path)

    # Measure the actual first-open migration on a populated legacy database.
    original_path = db.DB_PATH
    try:
        db.DB_PATH = indexed_path
        start = time.perf_counter()
        indexed = db.get_connection()
        first_open_ms = (time.perf_counter() - start) * 1000
        indexed.close()
        start = time.perf_counter()
        indexed = db.get_connection()
        second_open_ms = (time.perf_counter() - start) * 1000
    finally:
        db.DB_PATH = original_path

    baseline = sqlite3.connect(baseline_path)
    try:
        result = {
            "events": count,
            "baseline_bytes": baseline_path.stat().st_size,
            "indexed_bytes": indexed_path.stat().st_size,
            "first_open_ms": first_open_ms,
            "second_open_ms": second_open_ms,
            "reads": {},
            "writes": {},
        }
        for name, function in QUERIES.items():
            before, before_stats = read_trials(baseline, function)
            after, after_stats = read_trials(indexed, function)
            # Client ties have unspecified ordering. Compare the complete rows.
            key = repr if name == "list_known_clients" else None
            assert sorted(before, key=key) == sorted(after, key=key)
            expected_index = INDEX_NAMES[0 if name == "event_counts" else 1]
            assert any(expected_index in step for step in after_stats["plan"])
            assert not any("TEMP B-TREE FOR GROUP BY" in step for step in after_stats["plan"])
            result["reads"][name] = {
                "groups": len(after),
                "baseline": before_stats,
                "indexed": after_stats,
                "speedup": before_stats["median_ms"] / after_stats["median_ms"],
            }
        for name, conn in (("baseline", baseline), ("indexed", indexed)):
            result["writes"][name] = {
                "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
                **write_trials(conn, writes),
            }
        return result
    finally:
        baseline.close()
        indexed.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", nargs="+", type=int, default=[10000, 100000])
    parser.add_argument("--writes", type=int, default=300)
    args = parser.parse_args()
    if min(args.events) < 1 or args.writes < 2:
        parser.error("events must be positive and writes must be at least two")
    print(json.dumps({"platform": platform.platform(), "python": platform.python_version(),
                      "sqlite": sqlite3.sqlite_version}))
    with tempfile.TemporaryDirectory(prefix="plex-index-benchmark-") as directory:
        for count in args.events:
            print(json.dumps(benchmark(Path(directory), count, args.writes), indent=2))


if __name__ == "__main__":
    main()
