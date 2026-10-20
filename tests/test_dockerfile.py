"""The image installs with --only-binary, so the base Python must have wheels for every locked package."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Newest Python minor for which every package in requirements.lock ships a binary
# wheel. PyYAML (a direct dependency) has no cp315 wheel yet, so a 3.15 base makes
# `pip install --only-binary :all:` fail. Raise this only after confirming the new
# minor installs from the lock (docker build .), regenerating the locks with
# scripts/lock_requirements.sh if pins had to move.
MAX_PYTHON_WITH_LOCKED_WHEELS = (3, 14)


def _base_python():
    first_from = next(
        line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("FROM ")
    )
    match = re.search(r"python:(\d+)\.(\d+)", first_from)
    assert match, f"Dockerfile base is not a python:<major>.<minor> image: {first_from!r}"
    return int(match[1]), int(match[2])


def test_dockerfile_base_python_has_wheels_for_the_lock():
    assert _base_python() <= MAX_PYTHON_WITH_LOCKED_WHEELS, (
        "Dockerfile base Python is newer than the locked dependencies' binary wheels support; "
        "see MAX_PYTHON_WITH_LOCKED_WHEELS in tests/test_dockerfile.py"
    )
