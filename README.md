# plex-webhook

A Plex Pass webhook receiver that captures play/pause/stop/rate events and
can dim/restore lights per room based on which Plex client is playing.

Phases 1–3 are built. Open work is tracked as GitHub issues, mirrored from
`docs/backlog.yaml` (finished items stay there marked `status: done`):

- [#2](https://github.com/dflippojr/plex-webhook/issues/2) wire live `rooms.yaml` and device credentials, then dim on real playback
- [#3](https://github.com/dflippojr/plex-webhook/issues/3) verify Govee LAN control against a real bulb (blocked on #2)
- [#4](https://github.com/dflippojr/plex-webhook/issues/4) verify Tuya/Gosund DPS indices against a real device (blocked on #2)

The next planned steps are those three hardware checks; they need the owner's devices
and accounts. Other open issues (for example #13, #17, #19) cover deployment and repo upkeep.

## Run

```
docker compose up -d --build
```

Binds to `127.0.0.1:9800` only (loopback) since Plex Media Server runs on
the same host — no need to expose this beyond localhost.

The container runs as an unprivileged user (uid 10001), so `./data` must
be writable by it. Docker Desktop bind mounts on Windows already are; on a
Linux host, run `sudo chown -R 10001 data` once.

Dependencies install from `requirements.lock`, which locks every
transitive package with hashes. After editing the direct pins in
`requirements.txt`, regenerate it with `./scripts/lock_requirements.sh`
(needs Docker) and commit both files.

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
  the exact identifiers to put in your local `config/rooms.yaml`.
- `GET /rooms` — the currently loaded room config.
- `POST /rooms/validate` — check the configured room file without activating it.
- `POST /rooms/reload` — reload `config/rooms.yaml` without restarting the
  container (edits to the file otherwise only take effect on restart).

Both room operations read the same file (`ROOMS_CONFIG_PATH`, default
`/config/rooms.yaml`). Validation returns HTTP 200 with
`{"status":"valid","rooms":["living_room","bedroom"]}`; reload retains
`{"status":"reloaded","rooms":["living_room","bedroom"]}`. Validation never
changes the active mapping, playback state, gauges, or lights. Reload publishes
the mapping only after the entire candidate succeeds; it does not reconcile
already active playback after a remapping.

Invalid YAML or schema returns HTTP 422, for example:

```json
{"detail":[{"path":"rooms.den.plex_clients[0]","code":"expected_mapping"}]}
```

Paths identify the field to fix (`$` means the document root). Codes include
`invalid_yaml`, `recursive_yaml`, `duplicate_key`, `expected_mapping`,
`expected_list`, `expected_string`, `expected_string_or_null`, and
`expected_nonempty_string`. `conflicting_client` reports both entries when a
UUID or a trimmed, lowercase nonempty title belongs to different rooms.
Repeated identifiers within a single room are allowed; UUID matches take
precedence over titles. Errors omit client identifiers, light values, YAML
source fragments, and filesystem details.

The root, `rooms`, and each room must be mappings. When present, `plex_clients`
and `lights` must be lists of mappings. Client UUID/title and optional light
name/model accept strings or null; light brand/id require nonempty strings.
Extra fields and unknown light brands are preserved. Empty files, omitted
`rooms`, and `rooms: {}` are valid empty configurations.

A missing or unreadable file returns HTTP 503 with
`{"detail":"Rooms configuration unavailable"}`. Every failed validate/reload
preserves the last valid mapping and dispatcher state. Invalid startup config
logs a sanitized error and starts with empty mappings, keeping raw webhook
capture available; a missing startup file also leaves dispatch disabled.

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

Copy `config/rooms.yaml.example` to `config/rooms.yaml` (gitignored — it
will hold real Plex client UUIDs). That file maps each room to the Plex
clients that live there and the lights that should react:

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
room in the local `config/rooms.yaml`, then validate and reload:

```powershell
curl.exe -X POST http://127.0.0.1:9800/rooms/validate
# Only after validation returns 200 with status "valid":
curl.exe -X POST http://127.0.0.1:9800/rooms/reload
```

Fix any reported errors and validate again before reloading. Do not
commit that file.

Dispatch logic (`app/dispatcher.py`): a room tracks the set of clients
currently playing in it. The first client to start playing triggers a
`dim` action for every light in that room; the last client to
pause/stop triggers `restore`. Multiple simultaneous clients in the same
room are handled correctly (lights only restore once *all* of them have
stopped).

Light actions run on a single background worker thread, in arrival order, so a slow
or unreachable light never blocks `/health`, `/metrics` or the next webhook; a failed
action is logged and does not stop later ones. Tests: `pip install -r requirements-dev.txt && pytest`.

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

## Tests

An offline pytest suite covers the webhook receiver, the room dispatcher, the
SQLite event store and the Govee and Tuya controllers. Devices, `requests` and
`tinytuya` are replaced by fakes, and any non-loopback network access fails
the test. No `/data`, `/config` or `.env` is needed: the suite points the
paths at a temporary directory through the `DATA_DIR`, `DB_PATH` and
`ROOMS_CONFIG_PATH` environment variables (unset, they default to `/data`,
`/data/plex_events.db` and `/config/rooms.yaml`, as in the container).

```bash
pip install -r requirements-dev.txt   # Python 3.12; netifaces (a tinytuya dependency) needs a C compiler
pytest --cov=app --cov-report=xml
```

CI runs the same command on GitHub-hosted runners, not in the Docker image.

## Code analysis

SonarCloud is the main analysis path. `.github/workflows/sonar.yml` scans
every pull request and every push to `master` on GitHub-hosted runners
(project `dflippojr_plex-webhook`). The quality gate is informational for
now, so the check fails only if the scan itself errors. Pull requests from
forks skip the scan. When `SONAR_TOKEN` is absent, CI emits a notice and a
job summary explaining that no analysis ran; a green workflow in that case
does not establish a passing SonarCloud quality gate. The offline pytest suite
runs first (see [Tests](#tests)) and its `coverage.xml` is passed to the scan.

Store an authorized analysis token as `SONAR_TOKEN` in both repository secret
stores under **Settings → Secrets and variables**: **Actions** for ordinary
PRs and pushes, and **Dependabot** for Dependabot-triggered PRs. GitHub selects
the appropriate store for the triggering actor; Actions secrets are unavailable
to Dependabot runs. Never put token values in configuration or logs.

`.github/dependabot.yml` schedules weekly updates at the repository root for
pip (`requirements.txt`, `requirements-dev.txt` and the compiled requirements),
Docker (`Dockerfile` and `docker-compose.yml`) and GitHub Actions (workflow
action references). Review Python updates against `requirements.lock`, which
the production image installs; regenerate it with `./scripts/lock_requirements.sh`
if an update changes direct pins without refreshing the lockfile.

After merging the configuration, verify a default-branch scan and a real
Dependabot PR scan in Actions, then confirm the matching commit/PR analysis
in [SonarCloud](https://sonarcloud.io/project/overview?id=dflippojr_plex-webhook).
Adding a secret alone does not publish analysis; rerun the affected workflow.
CI configuration changes need no live-stack commands or container restart.

### Optional: local SonarQube (tower)

`sonar-project.properties` is set up for a tower-local SonarQube instance
(project key `plex-webhook`). To scan from the tower:

```powershell
D:\Docker\sonarqube\scan.ps1 -Path D:\Docker\plex-webhook -ProjectKey plex-webhook
```

See `D:\Docker\sonarqube\README.md`.
