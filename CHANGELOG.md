# Changelog

All notable changes to this service are documented here.

## [Unreleased] — 2026-06-20

### Added
- **HomeKit on/off switch for viewport auto-switching.** A "Viewport Auto-Switch"
  tile in the Home app turns motion-driven Live View switching on and off without
  touching the container.
  - New endpoints on the service (port 8686):
    - `POST /switch/on` — enable auto-switching.
    - `POST /switch/off` — disable, and immediately revert any currently held view.
    - `GET /switch/state` — returns `1` (enabled) or `0` (disabled); plain text for
      the Homebridge switch's status poll.
  - The enabled/disabled flag is persisted to `STATE_FILE`
    (`/app/logs/switch_state.json`, on the mounted `./logs` volume) so it survives
    container restarts. Default is **enabled**.
  - Endpoints are guarded by an optional `SWITCH_TOKEN` bearer token (sent as
    `Authorization: Bearer <token>`); empty disables auth.
  - Driven by a stateful `homebridge-http-switch` accessory added to the existing
    already-paired Homebridge bridge at
    `/home/warren/unifi-protect-privacy/homebridge/config.json` — appears as a new
    tile with no re-pair needed.
  - New config vars: `SWITCH_TOKEN`, `STATE_FILE` (see `.env.example`).

### Changed
- **Webhook response reflects the switch state.** When auto-switching is disabled,
  `POST /webhook` now returns `{"triggered": false, "reason": "auto-switching
  disabled", ...}`. The status stays **200** so UniFi Protect's Alarm Manager still
  treats the delivery as successful — no webhook reconfiguration required.
- **Default `ALARM_TIMEOUT` lowered from 30s to 7s.** Viewports now revert to the
  previous Live View 7 seconds after the last motion. Overridable via `ALARM_TIMEOUT`
  in `.env`.

## [0.1.0] — 2026-06-19

### Added
- Initial release: UniFi Protect Alarm Manager webhook → Viewport Live View
  switcher. Snapshots each configured viewport's current view, switches all to the
  requested Live View on motion, and restores after `ALARM_TIMEOUT`. Supports
  multiple comma-separated viewports and handles the Alarm Manager "Test" button
  envelope.
