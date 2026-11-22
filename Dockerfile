FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

WORKDIR /app

# requirements.lock pins every transitive dependency with hashes; regenerate
# it with scripts/lock_requirements.sh after editing requirements.txt.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes --only-binary :all: \
    -r requirements.lock

# Run as an unprivileged user. /data is pre-created and owned by it so the
# event log and SQLite DB stay writable when no volume is mounted there.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

COPY app/ ./app/

USER app

# In-flight requests get 3 s after SIGTERM, then the lifespan gets up to 5 s for queued light
# actions (app.service.SHUTDOWN_DRAIN_SECONDS): together under Docker's default 10 s stop timeout.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "3"]
