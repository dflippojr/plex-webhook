import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile  # noqa: E402

from app import db  # noqa: E402

# app.main opens the SQLite database at import time; keep it out of /data.
_DATA_DIR = Path(tempfile.mkdtemp(prefix="plex-webhook-tests-"))
db.DB_PATH = _DATA_DIR / "plex_events.db"
