# Phase 3 setup: gathering light-control credentials

`app/lights.py` implements real Govee and Tuya control, but it needs
credentials that only you can obtain (they're tied to your accounts and
local devices). None of this is required for the webhook/dispatcher itself
to run - with no credentials configured, light control calls simply log a
warning and do nothing.

This is a one-time setup per device/account. Do it whenever convenient;
the service works fine before, during, and after.

## 1. Govee Developer API key (cloud fallback)

1. Open the Govee Home app on your phone.
2. Go to **Profile -> Settings** (or **About Us**, depending on app
   version) **-> Apply for API Key**.
3. Fill in the short form. Approval is usually within ~2 business days;
   you'll get the key by email.
4. Copy `.env.example` to `.env` and set:
   ```
   GOVEE_API_KEY=your-key-here
   ```

## 2. Enable Govee LAN Control (primary, per-device)

LAN control is faster and doesn't depend on Govee's cloud being up, but
it's off by default and only supported on some models.

1. In the Govee Home app, open the device.
2. Go to its settings (gear icon) -> **LAN Control** -> enable it.
3. If the device doesn't have a LAN Control toggle, it doesn't support
   it - the code will automatically fall back to the Cloud API for that
   device as long as `GOVEE_API_KEY` is set and `model` (SKU) is filled
   in for it in `config/rooms.yaml` (copy from `config/rooms.yaml.example`
   if you haven't already; that live file is gitignored).
4. Note the device's SKU/model, shown in the app under the device's
   settings (e.g. `H6159`) - put it in the `model` field for that light in
   `config/rooms.yaml`.

If your network blocks UDP multicast between the machine running this
container and the bulbs (VLANs, AP isolation, Docker network mode), LAN
discovery won't find them. In that case, give the bulb a DHCP reservation
and add a static `ip` for it in `config/devices.secrets.yaml` (see
`config/devices.secrets.yaml.example`) instead of relying on discovery.

## 3. Tuya IoT Platform + `tinytuya wizard` (local control + cloud fallback)

Gosund/Tuya-based plugs and bulbs need a one-time cloud project link to
extract each device's local key, even though day-to-day control after
that is local (LAN).

1. Create a free account at https://iot.tuya.com.
2. Create a **Cloud Project** (Development, any datacenter close to you -
   note which region you pick, e.g. `us`/`eu`/`cn`/`in`, you'll need it).
3. Under the project's **Devices** tab, link your **Smart Life** or
   **Tuya Smart** app account (the one your Gosund devices are already
   set up in): click **Link App Account**, then scan the QR code from
   inside the Smart Life/Tuya Smart app (Me -> tap the QR icon at
   top-right -> scan). Your devices should then appear under the project.
4. Note the project's **Access ID** and **Access Secret** from the
   project overview page. Put them in `.env`:
   ```
   TUYA_ACCESS_ID=your-access-id
   TUYA_ACCESS_KEY=your-access-secret
   ```
5. Run the interactive wizard from a machine with Python (this needs your
   own Tuya account login/2FA, so it can't be automated for you):
   ```
   pip install tinytuya
   python -m tinytuya wizard
   ```
   It'll ask for the Access ID/Secret and region from step 4, then your
   Tuya app login, and will write a `devices.json` listing every linked
   device with its `id`, local `key`, and `ip`.
6. Transcribe each device from `devices.json` into
   `config/devices.secrets.yaml` (copy from
   `config/devices.secrets.yaml.example` if you haven't already):
   ```yaml
   devices:
     "<id from devices.json>":
       local_key: "<key from devices.json>"
       ip: "<ip from devices.json>"
   ```
   Use that same `id` as the light's `id` in `config/rooms.yaml`.

Device IPs can change if your router reassigns DHCP leases - if local
control starts failing for a device that used to work, re-check its IP
(the wizard's output, your router's client list, or just re-run the
wizard) and update `config/devices.secrets.yaml`.

## 3b. Home Assistant hub (`brand: hass`)

A light that Home Assistant already controls can be dimmed through the hub
instead of the Govee/Tuya transports. In `config/rooms.yaml`, give it
`brand: hass` and its Home Assistant entity id as `id` (for example
`light.todo_lamp`: `light.` then lowercase letters, digits and `_` only;
anything else is skipped as `invalid_entity_id`).

1. In Home Assistant, open your user profile, go to the **Security** tab and
   under **Long-lived access tokens** choose **Create token**. Copy it once;
   Home Assistant will not show it again. A dedicated non-admin user for this
   service limits what the token can do.
2. In `.env`, set `HASS_URL` to the hub's base URL (`http://` or `https://`,
   host and port, no query or fragment) and `HASS_TOKEN` to the token.
3. The container must be able to reach that URL. `localhost` inside the
   container is the container itself, not the host: use an address the
   container can resolve and route to. Check from the host with
   `docker compose exec plex-webhook python -c "import os, requests; print(requests.get(os.environ['HASS_URL'] + '/api/', headers={'Authorization': 'Bearer ' + os.environ['HASS_TOKEN']}, timeout=5).status_code)"`,
   which prints `200` when the URL and token work.

The token is sent only in the `Authorization` header. It is never logged,
audited or returned by any route, and redirects are not followed, so it is
never sent to another address. A non-2xx reply is audited as
`failed`/`hub_rejected`; a 2xx reply means the hub accepted the call, not
that the bulb changed. Home Assistant has no call that sets brightness while
leaving an off light off, so a dim of a hass light whose state could not be
read sends nothing (`skipped`/`brightness_only_unsupported`). Rotate the token
by editing `.env` and restarting (step 5).

## 4. Map rooms and Plex clients

This part is unchanged from the original Phase 3 workflow - see the
"Phase 3" section of `README.md`: copy `config/rooms.yaml.example` to
`config/rooms.yaml` if needed, play something on the target device,
hit `GET /clients` (send `Authorization: Bearer <ADMIN_API_TOKEN>`) to read off its real Plex `title`/`uuid`, fill in
the local `config/rooms.yaml`, then `POST /rooms/reload`. Do not commit
`rooms.yaml`.

## 4b. Admin API token

Set `ADMIN_API_TOKEN` in `.env` (16+ characters, e.g.
`python -c "import secrets; print(secrets.token_urlsafe(32))"`). It guards
`/rooms`, `/clients`, `/rooms/validate` and `/rooms/reload`; until it is set
those routes return 503. Rotate by editing `.env` and redeploying (step 5).
Never reuse Govee/Tuya keys. The token identifies the client `owner-admin`, not
a person; see README "Route permissions".

## 5. Restart with real credentials

```
docker compose up -d --build
```

(`.env` is read at container start, and `config/devices.secrets.yaml` is
cached in-process after its first read - restart the container after
editing either one for changes to take effect. `POST /rooms/reload` is
only for `config/rooms.yaml`.)
