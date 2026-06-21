# unifiprotect-viewport

Auto-switch a **UniFi Protect Viewport** to a camera's **Live View** when that
camera detects motion (via an Alarm Manager webhook), then revert to whatever
Live View was showing before, after a timeout.

Part of the `unifiprotect-*` webhook-service family on this host. Same skeleton
as govee / sprinkler / trimlight; the device client (`ViewportClient`) talks to
the Protect API (`/proxy/protect/api/...`) using the session-cookie + CSRF login
borrowed from `unifi-protect-privacy`.

## How it works

- `GET /proxy/protect/api/bootstrap` lists `viewers` (the Viewports) and
  `liveviews` (the saved camera views). On startup the service resolves the
  configured `VIEWPORT_NAME`(s) to viewer ids and builds a `view name -> id` map.
- On `POST /webhook?view=<Live View name>`, it snapshots each viewport's current
  Live View, `PATCH`es every configured viewer to the requested view, and arms a
  timer to restore each snapshot after `ALARM_TIMEOUT` seconds.
- **Multiple viewports:** `VIEWPORT_NAME` is a comma-separated list. One webhook
  switches the whole set together; each viewport reverts to its own previous view.
- Re-triggers (more motion) reset the timer and follow the newest camera, but the
  saved "previous view" stays the one captured on the first trigger, so it always
  reverts to where you actually left it.

## HomeKit on/off switch

A **"Viewport Auto-Switch"** tile in the Home app turns motion-driven switching on
and off, so you can stop the viewports flipping around (e.g. while watching TV)
without touching the container. It's driven by the already-paired Homebridge
bridge on this host (`homebridge-http-switch`, stateful) calling these endpoints:

| Endpoint | Purpose |
|----------|---------|
| `POST /switch/on` | enable auto-switching |
| `POST /switch/off` | disable (and immediately revert any held view) |
| `GET /switch/state` | `1` if enabled, `0` if disabled (for the switch's status poll) |

The flag is persisted to `STATE_FILE` (on the mounted `./logs` volume) so it
survives restarts. When `SWITCH_TOKEN` is set, all three endpoints require an
`Authorization: Bearer <token>` header — the Homebridge accessory sends the
matching header. While disabled, motion webhooks are accepted but ignored (200,
logged). The accessory lives in the live Homebridge `config.json`; adding it
needs no re-pair (it appears as a new tile inside the existing bridge).

```bash
curl -X POST http://localhost:8686/switch/off -H 'Authorization: Bearer <token>'
curl     http://localhost:8686/switch/state -H 'Authorization: Bearer <token>'  # -> 0
```

## Setup

1. **In Protect:** create one single-camera **Live View** per camera you want
   shown. When creating it, enable **"Share Multi-View Grid"** so the view is
   *global* — otherwise the API account (and the viewports) can't see it, and the
   switch fails with "unknown Live View". ("Share with Protect Viewport" only pins
   it to one viewport and is the wrong choice for API-driven switching.) Ensure a
   **local admin** account exists (Ubiquiti cloud SSO won't authenticate the API).
2. `cp .env.example .env` and fill in `UNIFI_HOST`, `UNIFI_USER`,
   `UNIFI_PASSWORD`, and `VIEWPORT_NAME` (comma-separate for multiple viewports).
3. `docker compose up -d --build`
4. `curl -s http://localhost:8686/` — status page lists the discovered Live View
   names and the viewports being driven.
5. **In Protect → Alarm Manager:** add one Webhook alarm per camera (motion /
   person / vehicle) → `POST http://<docker-host>:8686/webhook?view=<that
   camera's Live View name>` (URL-encode spaces, e.g. `Front%20Door`).

> **Adding a view later:** create + share it in Protect, then
> `docker compose restart` — the service reads the Live View list only at startup.

## Test without a camera

```bash
# Switch to a view, then watch it revert after ALARM_TIMEOUT:
curl -X POST 'http://localhost:8686/webhook?view=Front%20Door' \
  -H 'Content-Type: application/json' \
  -d '{"alarm":{"triggers":[{"key":"motion"}]}}'

# Empty/health POST is ignored:
curl -X POST http://localhost:8686/webhook -d '{}'
```

## Config (`.env`)

| Var | Required | Notes |
|-----|----------|-------|
| `UNIFI_HOST` | yes | `https://<console>` running Protect |
| `UNIFI_USER` / `UNIFI_PASSWORD` | yes | local admin (not cloud SSO) |
| `VIEWPORT_NAME` | yes* | Viewport name(s) in Protect, comma-separated for multiple (*or `VIEWER_ID`) |
| `VIEWER_ID` | no | pin viewer id(s) explicitly, comma-separated |
| `VERIFY_SSL` | no | default `false` (self-signed cert) |
| `WEBHOOK_PORT` | no | default `8686` |
| `ALARM_TIMEOUT` | no | seconds before reverting, default `7` |
| `SWITCH_TOKEN` | no | bearer token guarding `/switch/*` (must match the Homebridge accessory); empty = no auth |
| `STATE_FILE` | no | where the enable/disable flag is persisted, default `/app/logs/switch_state.json` |
| `LOG_LEVEL` / `LOG_FILE` | no | standard logging block |

After editing `service.py`, rebuild: `docker compose up -d --build` (the source is
baked into the image — a plain restart runs the old code).

## Gotchas

- **Live Views must be global.** The API user only sees views with `isGlobal=True`.
  Create them with **Share Multi-View Grid** (see Setup). A private view returns
  nothing from `/proxy/protect/api/liveviews` and the switch fails.
- **Alarm Manager's "Test" button sends a different payload than a real
  detection.** A real detection POSTs `{"alarm":{...,"triggers":[...]}}` directly;
  the Test button wraps your body as an escaped string inside
  `{"alarm_id":"TEST","text":"..."}`. The service unwraps a top-level `text` field
  so both work — but a hand-crafted `curl` (see above) is the most reliable test.
- **Network:** Protect (the NVR) must reach this host on `WEBHOOK_PORT`. If the
  host runs a firewall, open the port (e.g. `firewall-cmd --add-port=8686/tcp
  --permanent && firewall-cmd --reload`).
