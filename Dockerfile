FROM python:3.12-slim

WORKDIR /app

# build-essential is needed to compile the `netifaces` extension (a
# tinytuya dependency, used for local Tuya device discovery) - there's no
# prebuilt wheel for it on Python 3.12.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

# requirements.lock pins every transitive dependency with hashes; regenerate
# it with scripts/lock_requirements.sh after editing requirements.txt.
# Everything installs from wheels except netifaces (see above).
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes \
    --only-binary :all: --no-binary netifaces \
    -r requirements.lock

# Run as an unprivileged user. /data is pre-created and owned by it so the
# event log and SQLite DB stay writable when no volume is mounted there.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

COPY app/ ./app/

USER app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
