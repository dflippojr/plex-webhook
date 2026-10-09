FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

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

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
