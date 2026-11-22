"""Deadlines for shutdown work: a ``time.monotonic()`` instant, or None for no limit.

Every blocking step a shutdown can reach after its drain starts takes one (see
tests/test_shutdown_budget.py): it waits only for the time left and raises
DeadlineExceeded once none is.
"""
import time


class DeadlineExceeded(Exception):
    """The shutdown budget ran out before this step could run; nothing was written."""


def remaining(deadline):
    """Seconds left before ``deadline`` (None for no limit); raises DeadlineExceeded when none are."""
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        raise DeadlineExceeded
    return left


def acquire(lock, deadline):
    """Acquire ``lock``, waiting at most until ``deadline``; raises DeadlineExceeded instead of waiting longer."""
    left = remaining(deadline)
    if not lock.acquire(timeout=-1 if left is None else left):
        raise DeadlineExceeded
