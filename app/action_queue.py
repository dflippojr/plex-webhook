"""One worker thread that applies queued light actions in order."""
import logging
import threading
import time
from collections import deque
from concurrent.futures import Future

logger = logging.getLogger("plex-webhook")


class ActionQueue:
    """One worker that applies queued actions in order, coalescing them by key.

    Submitting with a key drops any not-yet-started entry with the same key, and the
    new entry goes to the back, so the latest state for a room wins and still runs after
    every earlier action for that room. When the queue holds ``limit`` entries the oldest
    queued one is dropped. A dropped entry's Future is cancelled and its ``on_drop``
    callback is called with ``"superseded"`` or ``"queue_overflow"`` on the submitting
    thread, outside the lock. With ``evict=False`` a full queue drops the new entry
    instead, so optional work never pushes out a queued light action.

    ``close`` stops intake and lets the worker finish for a bounded time; whatever is
    still queued then, and anything submitted after it, is dropped with ``"shutdown"``.
    """

    def __init__(self, limit, name="light-action"):
        self.limit = limit
        self._name = name
        self._entries = deque()
        self._ready = threading.Condition()
        self._worker = None
        self._running = False
        self._closed = False

    def submit(self, fn, *args, key=None, on_drop=None, evict=True, **kwargs):
        future = Future()
        dropped = []
        entry = (key, future, fn, args, kwargs, on_drop)
        with self._ready:
            if self._closed:
                dropped.append((entry, "shutdown"))
            else:
                self._enqueue(entry, evict, dropped)
        _drop(dropped)
        return future

    def _enqueue(self, entry, evict, dropped):
        """Queue ``entry`` under the lock, collecting what it supersedes or pushes out."""
        key = entry[0]
        if key is not None:
            for queued in [queued for queued in self._entries if queued[0] == key]:
                self._entries.remove(queued)
                dropped.append((queued, "superseded"))
        if not evict and len(self._entries) >= self.limit:
            dropped.append((entry, "queue_overflow"))
            return
        while len(self._entries) >= self.limit:
            dropped.append((self._entries.popleft(), "queue_overflow"))
        self._entries.append(entry)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._worker.start()
        self._ready.notify()

    def pending(self):
        with self._ready:
            return len(self._entries)

    def drain(self, timeout=None):
        """Block until every action queued before this call has finished; returns the marker's result."""
        return self.submit(lambda: None).result(timeout=timeout)

    def close(self, timeout):
        """Stop intake and wait up to ``timeout`` seconds for the queue to drain; True when it did.

        Entries still queued at the deadline are dropped with ``"shutdown"`` (their ``on_drop``
        runs on this thread). An action already running is never interrupted: if it outlasts
        the deadline it keeps no terminal record unless it finishes before the process exits.
        """
        deadline = time.monotonic() + timeout
        with self._ready:
            self._closed = True
            self._ready.notify_all()
            while self._entries or self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._ready.wait(remaining)
            dropped = [(entry, "shutdown") for entry in self._entries]
            self._entries.clear()
            drained = not dropped and not self._running
        _drop(dropped)
        return drained

    def _run(self):
        while True:
            with self._ready:
                self._running = False
                self._ready.notify_all()
                while not self._entries:
                    if self._closed:
                        return
                    self._ready.wait()
                _, future, fn, args, kwargs, _ = self._entries.popleft()
                self._running = True
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)


def _drop(dropped):
    for (_, dropped_future, _, _, _, callback), reason in dropped:
        dropped_future.cancel()
        if callback is not None:
            try:
                callback(reason)
            except Exception:
                logger.error("reason=dispatcher_action_failed operation=drop")
