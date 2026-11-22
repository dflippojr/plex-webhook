"""Every blocking step a shutdown can reach takes the remaining budget (#185, r2).

``Service.stop()`` drains the light queue (``ActionQueue.close``) and then runs the drop
callbacks, whose only blocking work is the terminal audit write (``audit.append``). The
inventory below classifies every lock acquire, condition/thread/future wait and SQLite
connection (busy timeout) in ``app/``: either it is on that shutdown path and takes a
``deadline``, or it is not reachable from it, with the reason. A new site fails the test
until it is classified, so the class of "an unbounded wait inside the shutdown deadline"
stays closed.
"""
import ast
import inspect
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app import audit, deadline as deadlines
from app.action_queue import ActionQueue

APP = Path(__file__).resolve().parent.parent / "app"
BLOCKING_CALLS = {"acquire", "wait", "join", "result", "connect", "_connect"}

# (module, function, what blocks) -> how a shutdown is protected from it.
ON_SHUTDOWN_PATH = {
    ("action_queue", "ActionQueue.close", "wait"): "the drain wait ends at timeout - drop_budget",
    ("audit", "append", "WRITE_LOCK"): "acquired with the remaining budget (deadline.acquire)",
    ("audit", "append", "acquire"): "the deadline.acquire call for WRITE_LOCK",
    ("audit", "append", "connect"): "SQLite busy timeout capped by the remaining budget",
    ("deadline", "acquire", "acquire"): "the budgeted lock helper: waits only for the remaining budget",
}
NOT_ON_SHUTDOWN_PATH = {
    ("action_queue", "ActionQueue.drain", "result"): "tests and callers only; close never drains this way",
    ("action_queue", "ActionQueue._run", "wait"): "the worker thread, never the thread running stop()",
    ("db", "insert_event", "WRITE_LOCK"): "webhook handler; requests are finished before the lifespan stops",
    ("db", "RestoreRecords._connect", "connect"): "restore records are read and written on the worker thread",
    ("db", "RestoreRecords.put_restore_record", "WRITE_LOCK"): "worker thread",
    ("db", "RestoreRecords.put_restore_record", "_connect"): "worker thread",
    ("db", "RestoreRecords.delete_restore_record", "WRITE_LOCK"): "worker thread",
    ("db", "RestoreRecords.delete_restore_record", "_connect"): "worker thread",
    ("db", "RestoreRecords.set_automation_owner", "WRITE_LOCK"): "worker thread",
    ("db", "RestoreRecords.set_automation_owner", "_connect"): "worker thread",
    ("db", "RestoreRecords.get_restore_record", "_connect"): "worker thread and light checks",
    ("db", "RestoreRecords.automation_owner", "_connect"): "worker thread",
    ("db", "RestoreRecords.automation_targets", "_connect"): "worker thread",
    ("db", "RestoreRecords.automation_sources", "_connect"): "startup (load_pending_rooms)",
    ("db", "RestoreRecords.rooms_with_restore_records", "_connect"): "startup (load_pending_rooms)",
    ("storage", "connect", "connect"): "the helper itself; its callers are classified here",
    ("storage", "open_database", "connect"): "startup only",
    ("audit_cli", "open_database", "connect"): "the offline CLI, never the service",
}


def _sites():
    found = set()
    for path in sorted(APP.glob("*.py")):
        module = path.stem

        def visit(node, scope):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit(child, [*scope, child.name])
                    continue
                name = ".".join(scope)
                if isinstance(child, ast.Name) and child.id == "WRITE_LOCK" and scope:
                    found.add((module, name, "WRITE_LOCK"))
                if isinstance(child, ast.Call) and scope and _called(child) in BLOCKING_CALLS \
                        and not _is_string_join(child):
                    found.add((module, name, _called(child)))
                visit(child, scope)

        visit(ast.parse(path.read_text(encoding="utf-8")), [])
    return found


def _called(call):
    """The called name: ``x.wait(...)`` and ``wait(...)`` both give ``wait``."""
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _is_string_join(call):
    return isinstance(call.func, ast.Attribute) and call.func.attr == "join" \
        and isinstance(call.func.value, ast.Constant)


def test_every_lock_and_wait_site_in_app_is_classified():
    assert ON_SHUTDOWN_PATH.keys().isdisjoint(NOT_ON_SHUTDOWN_PATH)
    assert _sites() == ON_SHUTDOWN_PATH.keys() | NOT_ON_SHUTDOWN_PATH.keys()


def test_every_site_on_the_shutdown_path_takes_a_budget():
    from app import dispatcher
    from app.service import Service

    assert "drop_budget" in inspect.signature(ActionQueue.close).parameters
    assert "deadline" in inspect.signature(audit.append).parameters
    # The drop callback hands the budget on to the summary write.
    assert "deadline" in inspect.signature(dispatcher._summary).parameters
    assert "deadline" in inspect.signature(dispatcher._record).parameters
    assert Service.stop.__doc__ and "deadline" in Service.stop.__doc__


def _started_database(tmp_path):
    from app import storage

    path = tmp_path / "audit.db"
    storage.open_database(path).close()
    return path


def _summary_fields():
    return dict(action="light.action_summary", outcome="skipped", reason_code="shutdown",
                correlation=audit.correlation_id(), target_id="room",
                detail={"action_id": audit.correlation_id(), "request": "dim", "on_behalf_of": "plex_server",
                        "event_id": None, "counts": {}})


def test_audit_append_gives_up_on_a_held_write_lock_at_the_deadline(tmp_path):
    path = _started_database(tmp_path)
    held, release = threading.Event(), threading.Event()

    def hold():
        with audit.WRITE_LOCK:
            held.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(timeout=5)
    try:
        start = time.monotonic()
        with pytest.raises(deadlines.DeadlineExceeded):
            audit.append(path, **_summary_fields(), deadline=time.monotonic() + 0.2)
        assert time.monotonic() - start < 0.2 + 0.15
    finally:
        release.set()
        holder.join(timeout=5)


def test_audit_append_caps_the_sqlite_busy_wait_by_the_deadline(tmp_path):
    path = _started_database(tmp_path)
    locker = sqlite3.connect(path, timeout=0)
    locker.execute("BEGIN IMMEDIATE")
    try:
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            audit.append(path, **_summary_fields(), deadline=time.monotonic() + 0.05)
        assert time.monotonic() - start < 0.05 + 0.15  # not the 0.25 s default busy wait
    finally:
        locker.rollback()
        locker.close()


def test_audit_append_past_its_deadline_writes_nothing(tmp_path):
    path = _started_database(tmp_path)
    with pytest.raises(deadlines.DeadlineExceeded):
        audit.append(path, **_summary_fields(), deadline=time.monotonic() - 1)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)
    conn.close()
    assert audit.append(path, **_summary_fields()) == 1  # no deadline: the normal write


def test_a_drop_callback_that_runs_out_of_budget_is_counted_as_unaudited(caplog):
    queue, release, started = ActionQueue(limit=8, name="test-queue"), threading.Event(), threading.Event()

    def block():
        started.set()
        release.wait(timeout=10)

    def out_of_budget(reason, deadline):
        raise deadlines.DeadlineExceeded

    queue.submit(block)
    assert started.wait(timeout=5)
    queue.submit(lambda: None, on_drop=out_of_budget)
    try:
        assert queue.close(0.2, drop_budget=0.1) is False
    finally:
        release.set()
    assert "reason=shutdown_drops_unaudited count=1" in caplog.text
