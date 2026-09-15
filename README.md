# plex-webhook

A Plex Pass webhook receiver that captures play/pause/stop/rate events and
can dim/restore lights per room based on which Plex client is playing.

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

## Endpoints

- `POST /webhook` — Plex webhook target.
- `GET /health` — liveness check.
- `GET /metrics` — Prometheus metrics.
- `GET /clients` — every distinct Plex client (`title` + `uuid`) seen in
  captured events, with event count and last-seen time. Use this to find
  the exact identifiers to put in `config/rooms.yaml`.
- `GET /rooms` — the currently loaded room config.
- `POST /rooms/reload` — reload `config/rooms.yaml` without restarting the
  container (edits to the file otherwise only take effect on restart).

## Phase 1 — raw event capture

Every webhook delivery is appended as a JSON line to `./data/events.jsonl`:
`received_at`, `event` type, full decoded `payload`, and any attachment
metadata (e.g. thumbnail).

## Phase 2 — structured storage + metrics

Events are also parsed into `./data/plex_events.db` (SQLite, `events`
table) and exposed as Prometheus metrics (`plex_webhook_events_total`,
`plex_webhook_last_event_timestamp_seconds`), scraped by the existing
`observability-stack` Prometheus and shown on the "Plex Webhook" Grafana
dashboard in the "Basement PC" folder.

## Phase 3 — room-based light dispatcher

`config/rooms.yaml` manually maps each room to the Plex clients that live
there and the lights that should react:

```yaml
rooms:
  living_room:
    name: "Living Room"
    plex_clients:
      - title: "Living Room Apple TV"   # match by title, or...
        uuid: null                       # ...uuid, once known (more stable)
    lights:
      - brand: govee
        id: "device-id-once-known"
        name: "Couch Lamp"
```

Workflow to fill it in: play something on the target device, hit
`GET /clients` to read off its real `title`/`uuid`, add it under the right
room in `config/rooms.yaml`, then `POST /rooms/reload`.

Dispatch logic (`app/dispatcher.py`): a room tracks the set of clients
currently playing in it. The first client to start playing triggers a
`dim` action for every light in that room; the last client to
pause/stop triggers `restore`. Multiple simultaneous clients in the same
room are handled correctly (lights only restore once *all* of them have
stopped).

Light control (`app/lights.py`) now has real per-brand controllers:

- **`GoveeController`**: hybrid control — tries LAN control (UDP) first,
  falls back to the Govee Cloud API if LAN discovery/control fails or
  isn't supported on that device.
- **`TuyaController`** (covers Gosund-based lights): hybrid control via
  the `tinytuya` package — local control (device id + local key + IP)
  first, Tuya Cloud API fallback.

Both controllers catch every failure internally (missing credentials,
unreachable device, network error) and log a warning rather than raising,
so the dispatcher keeps working with zero devices configured. No real
credentials exist in this repo — see **[SETUP.md](SETUP.md)** for how to
gather your own Govee API key, enable Govee LAN Control per device, and
set up a Tuya IoT Platform project + run `tinytuya wizard` to get local
keys for your devices.

Dispatcher activity is also exported as Prometheus metrics
(`plex_dispatcher_room_active_sessions`, `plex_dispatcher_actions_total`)
and shown on the same Grafana dashboard.

## Local SonarQube (tower)

Scan from the tower (not from GitHub-hosted Actions):

```powershell
D:\Docker\sonarqube\scan.ps1 -Path D:\Docker\plex-webhook -ProjectKey plex-webhook
```

See `D:\Docker\sonarqube\README.md`. Public CI can use SonarCloud later.
