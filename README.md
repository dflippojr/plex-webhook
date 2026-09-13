# plex-webhook

Phase 1 of the Plex webhook event pipeline: a minimal FastAPI receiver that
captures raw Plex Pass webhook events (play/pause/stop/rate/etc.) to a
JSONL log for later processing.

## Run

```
docker compose up -d --build
```

Binds to `127.0.0.1:9800` only (loopback) since Plex Media Server runs on
the same host — no need to expose this beyond localhost.

## Configure in Plex

Plex web UI → Settings → Account → Webhooks → Add Webhook:

```
http://127.0.0.1:9800/webhook
```

(Requires Plex Pass, confirmed active on this account.)

## Output

Raw events are appended as JSON lines to `./data/events.jsonl`, one per
webhook delivery: `received_at`, `event` type, full decoded `payload`, and
any attachment metadata (e.g. thumbnail) that came with the multipart
request.

## Next phases (see project backlog)

- Phase 2: promote `events.jsonl` into SQLite + a Prometheus exporter +
  Grafana dashboard (alongside the existing `observability-stack`).
- Phase 3 (deferred): smart-home dispatcher reacting to events (e.g. dim
  lights on play) — direction still needs picking per-brand local APIs
  (Govee LAN control, Tuya local-key for Gosund-style devices) since
  Google Home has no webhook/automation path.
