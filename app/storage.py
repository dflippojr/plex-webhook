"""The one SQLite file: its connections, WAL mode and numbered schema migrations.

Standard library only, so app.audit and the offline CLI can import it. Service
startup calls open_database once: it switches the file to WAL and applies every
migration newer than the file's PRAGMA user_version. Every other connection
comes from connect(), which sets per-connection pragmas and runs no DDL.
"""
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("plex-webhook")

# How long a connection waits for another writer's lock before failing (seconds).
BUSY_TIMEOUT_S = 5

EVENTS_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    event TEXT,
    account_title TEXT,
    player_title TEXT,
    player_uuid TEXT,
    media_type TEXT,
    title TEXT,
    grandparent_title TEXT,
    rating_key TEXT,
    raw_payload TEXT NOT NULL,
    raw_truncated INTEGER NOT NULL DEFAULT 0,
    attachments TEXT
)""",
    """CREATE TABLE IF NOT EXISTS light_restore_records (
    room TEXT NOT NULL,
    light_id TEXT NOT NULL,
    was_off INTEGER NOT NULL CHECK(was_off IN (0, 1)),
    restore_percent INTEGER,
    dim_percent INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (room, light_id)
)""",
    """CREATE TABLE IF NOT EXISTS automation_room_owners (
    target_room TEXT PRIMARY KEY,
    trigger_room TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_events_event ON events(event)",
    "CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at)",
    # Replaced by idx_events_player_labels_cover: plain columns let the counter re-seed
    # read only the index, where the old expression index still looked up every row,
    # and player_uuid lets the re-seed resolve rooms by uuid the way dispatch does.
    "DROP INDEX IF EXISTS idx_events_counts_cover",
    "DROP INDEX IF EXISTS idx_events_labels_cover",
    """CREATE INDEX IF NOT EXISTS idx_events_player_labels_cover
    ON events(event, player_title, player_uuid, account_title)""",
    """CREATE INDEX IF NOT EXISTS idx_events_clients_cover
    ON events(player_title, player_uuid, received_at)
    WHERE player_title IS NOT NULL""",
)

AUDIT_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_id TEXT,
    actor_verified INTEGER NOT NULL CHECK(actor_verified IN (0, 1)),
    action TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    changed_fields TEXT NOT NULL,
    checksum TEXT NOT NULL,
    detail TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_events(correlation_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action, id)",
)


def _script(statements):
    return "".join(f"{statement};\n" for statement in statements)


# The same DDL as scripts, for tests and tools that build a table set directly.
EVENTS_SCHEMA = _script(EVENTS_STATEMENTS)
AUDIT_SCHEMA = _script(AUDIT_STATEMENTS)


def _create_tables(conn):
    for statement in EVENTS_STATEMENTS + AUDIT_STATEMENTS:
        conn.execute(statement)


def _add_column(table, name, definition):
    def step(conn):
        if name not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    return step


# Migration N is MIGRATIONS[N - 1]; a file at user_version N has run 1..N. Never edit
# or reorder a released step, including the DDL above that step 1 runs: append a new one.
# Steps 2-4 are the column checks that ran on every open before versioning, so a file
# from any earlier release upgrades exactly as it did then. Rows stored before a column
# existed keep its default: not truncated, attachments and detail unknown (NULL).
MIGRATIONS = (
    _create_tables,
    _add_column("events", "raw_truncated", "INTEGER NOT NULL DEFAULT 0"),
    _add_column("events", "attachments", "TEXT"),
    _add_column("audit_events", "detail", "TEXT"),
)


def connect(database, *, timeout=BUSY_TIMEOUT_S, uri=False):
    """A connection with the service's pragmas. It runs no DDL; open_database did that at startup."""
    conn = sqlite3.connect(database, timeout=timeout, uri=uri)
    # Under WAL, NORMAL syncs at checkpoints instead of on every commit. A power cut can
    # lose the last few commits but leaves the file consistent; a process crash loses none.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def enable_wal(conn):
    """Put the file in WAL mode, so readers never block writers; returns the resulting mode.

    The mode persists in the file, but startup sets it every time: a file restored from a
    backup, or converted back with journal_mode=DELETE, returns to WAL on the next start.
    A file that cannot switch (another process holds it, or a network filesystem) keeps
    working in its old mode, with a warning.
    """
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    except sqlite3.OperationalError:
        mode = None
    if mode != "wal":
        logger.warning("reason=wal_unavailable")
    return mode


def migrate(conn):
    """Apply every migration newer than the file's user_version, each in its own transaction.

    An up-to-date file takes no write lock. Each step re-reads the version under the
    write lock, so two processes starting at once never run a step twice. A file from
    newer code (a higher version) is left alone.
    """
    while _user_version(conn) < len(MIGRATIONS):
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            version = _user_version(conn)
            if version < len(MIGRATIONS):  # else another process ran it since the check above
                MIGRATIONS[version](conn)
                conn.execute(f"PRAGMA user_version = {version + 1}")


def _user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def open_database(path):
    """The service's startup connection: creates the file if needed, enables WAL and migrates."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    enable_wal(conn)
    migrate(conn)
    return conn
