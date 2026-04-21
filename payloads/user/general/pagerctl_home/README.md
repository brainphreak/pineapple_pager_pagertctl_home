# Pagerctl Home

Custom home screen for the WiFi Pineapple Pager — a fully themeable,
JSON-driven UI that replaces the stock Pineapple Pager interface
while keeping compatibility with its payload library.

## What it does

Pagerctl Home takes over the pager's display and input while running,
replacing the stock home screen with a theme-engine-rendered UI. It
owns the framebuffer via `libpagerctl.so`, serves the Hak5 duckyscript
API over `/tmp/api.sock` for payloads that need it, and dispatches
every screen/dialog/menu through JSON theme components so the whole
UI can be re-skinned without touching Python.

## Screens

- **Main dashboard** — animated home screen with battery, time,
  WiFi, GPS, Bluetooth status widgets.
- **Payloads browser** — scans `/mmc/root/payloads/user/` and lists
  every installed payload with a `pagerctl.sh`. Classic
  `payload.sh`-only payloads can be opted back in via
  **Settings > General > Show Classic Payloads**.
- **Reconnaissance, Alerts, PineAP dashboards** — themed overlays
  for each Hak5 tool.
- **Wardrive dashboard** — passive WiFi + GPS scanner with handshake
  capture and Wigle export, integrated as a native screen.
- **Captive portal dashboard** — two-column grid for captive portal
  attacks (AP, DNS hijack, cred captures, …) with live counter
  updates.
- **WiFi Attacks dashboard** — **WORK IN PROGRESS.** Captive-style
  grid hosting a growing set of WiFi offensive tools. Mode cell
  selects the attack; other cells configure it and show live
  counters. Current status per mode:
    - **SSID Spam** — partial. Hardware-limited by the pager's mt76
      driver to 1 BSSID per radio, so the classic rickroll-flood
      where 20+ APs appear at once isn't possible; instead one AP
      rotates SSIDs and occasionally flips BSSID to introduce new
      entries into scanner caches. Working but less dense than a
      stock-beacon-flood tool would be on injection-capable
      hardware. Dual-radio mode doubles density at the cost of
      temporarily dropping your WiFi client connection.
    - **Handshake Capture**, **Probe Monitor**, **Scan** — in
      progress, not wired up yet. The monitor-mode RX path they rely
      on works fine (proven by Wardrive), so these will land.
    - **Deauth**, **Karma**, **WPS Pixie-Dust** — **removed from
      the menu** because they require monitor-mode frame injection,
      which the mt76 driver on this hardware blocks silently
      (confirmed with 0 TX packets on test sends of both beacon
      and deauth frames). They'll come back if we find a driver
      workaround or ship on hardware with working injection.
  The dashboard shows a visible "(WIP Under Development)" banner on
  every mode until the backends are all functional.
- **System info** — live hardware + firmware dashboard.
- **Settings** — JSON-driven settings schema with toggles, cycles,
  actions, and themed on-screen keyboard for text input.
- **Captive-portal / on-screen keyboards** — full alphanumeric,
  numeric, hex, IP and MAC variants with password masking.
- **Payload runner** — themed log viewer + duckyscript dialog
  servicer (alert, confirm, list picker, text picker, IP/MAC/number
  pickers, spinner).
- **Power menu** — shutdown / restart / sleep.

## Installation

Drop the `pagerctl_home/` directory into
`/root/payloads/user/general/` on the pager. The payload shows up in
the Pineapple Pager UI under **General > Pagerctl Home**; launch it
once to start using the custom home screen.

To have it take over at boot, install the
[Pagerctl Bootloader](https://github.com/pineapple-pager-projects/pineapple_pager_bootloader)
and enable **Settings > Auto Boot > Pagerctl Home** in the bootloader.
On a fresh install, the bootloader auto-boot default is already set
to Pagerctl Home if it's detected.

## Configuration

- **Theme** — selected via the `PAGERCTL_THEME` environment variable,
  falling back to `themes/Circuitry` if unset. Drop a new theme
  directory under `themes/` and point the env var at it. See
  [THEMING.md](THEMING.md) for the theme authoring guide — component
  layout, central `color_palette` + `text_sizes`, creating a new
  theme.
- **Settings** — persisted in `wardrive/settings.json` (shared) and
  `settings.json` (local overrides). Schema is driven by
  `themes/<Active>/components/dashboards/settings_dashboard.json`;
  new settings items can be added there without touching Python.
- **Payloads** — see [PAYLOAD_AUTHORING.md](PAYLOAD_AUTHORING.md) for
  the `pagerctl.sh` contract, required header metadata, Python
  skeleton, and notes on classic / duckyscript compatibility.

## Controls

| Button | Action |
|--------|--------|
| D-pad | Navigate |
| A (GREEN) | Select / confirm |
| B (RED) | Back / cancel |
| POWER | Power menu |

Per-screen bindings are defined in each component's `button_map`
block; new themes can remap them.

## Related projects

- [Pagerctl Bootloader](https://github.com/pineapple-pager-projects/pineapple_pager_bootloader)
  — pre-boot launcher menu that can auto-boot straight into Pagerctl
  Home.
- [Loki](https://github.com/pineapple-pager-projects/pineapple_pager_loki),
  [Wardrive](https://github.com/brainphreak/pineapple_pager_wardrive),
  [CYT](https://github.com/pineapple-pager-projects/pineapple_pager_cyt),
  [PagerGotchi](https://github.com/pineapple-pager-projects/pineapple_pager_pagergotchi),
  [PagerAmp](https://github.com/pineapple-pager-projects/pineapple_pager_pageramp_mp3_player),
  [Tetris](https://github.com/pineapple-pager-projects/pineapple_pager_tetris),
  [Space Invaders](https://github.com/pineapple-pager-projects/pineapple_pager_space_invaders),
  [Hakanoid](https://github.com/pineapple-pager-projects/pineapple_pager_hakanoid)
  — all ship a `pagerctl.sh` and launch natively from Pagerctl Home.

## Docs

- [THEMING.md](THEMING.md) — theme architecture, components, palette,
  how to build a new theme.
- [PAYLOAD_AUTHORING.md](PAYLOAD_AUTHORING.md) — writing a payload
  that launches under Pagerctl Home.
