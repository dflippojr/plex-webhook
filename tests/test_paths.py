"""The env overrides must not change where the container looks by default."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = """
import app.db, app.rooms
print(app.db.DB_PATH.as_posix())
print(app.rooms.CONFIG_PATH.as_posix())
"""


def _run(env_extra):
    env = {k: v for k, v in os.environ.items() if k not in ("DATA_DIR", "DB_PATH", "ROOMS_CONFIG_PATH")}
    env.update(env_extra)
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_defaults_without_env():
    assert _run({}) == ["/data/plex_events.db", "/config/rooms.yaml"]


def test_env_overrides(tmp_path):
    db = tmp_path / "x.db"
    rooms = tmp_path / "r.yaml"
    assert _run({"DB_PATH": str(db), "ROOMS_CONFIG_PATH": str(rooms)}) == [db.as_posix(), rooms.as_posix()]
