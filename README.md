# Fronius Wattpilot for Home Assistant

[![Release](https://img.shields.io/github/v/release/Varitras/ha-wattpilot?sort=semver)](https://github.com/Varitras/ha-wattpilot/releases)
[![CI](https://github.com/Varitras/ha-wattpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Varitras/ha-wattpilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![HACS: custom](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz)

A local-push Home Assistant integration for **Fronius Wattpilot** wallbox
chargers. It talks to the charger over its WebSocket API on your own network —
no cloud account, no polling. The charger pushes a property, exactly the
entities that care about it update.

It is a **drop-in replacement** for the two existing community integrations,
[`ruaan-deysel/ha-wattpilot`][deysel] and [`mk-maddin/wattpilot-HA`][maddin]:
same `wattpilot` domain, same entity unique IDs, automatic config-entry
migration. Your history, dashboards, automations and entity customizations
survive the switch.

[deysel]: https://github.com/ruaan-deysel/ha-wattpilot
[maddin]: https://github.com/mk-maddin/wattpilot-HA

## Supported devices

Fronius Wattpilot chargers reachable on the local network, in both the 11 kW
and 22 kW variants. Developed and verified against a **Wattpilot Home 11 J 2.0
on firmware 42.5**; the Go models speak the same API but have not been tested
here. Older firmware is supported: entities that a given firmware or power
variant does not provide are simply not created, so no entity ever sits at a
permanently unknown state.

**Not supported yet:** cloud connections. A cloud-configured entry migrated
from an older integration will fail setup with a clear message asking you to
reconfigure it with the charger's local address.

## Migrating from ha-wattpilot or wattpilot-HA

1. Remove the old integration in **HACS → Integrations**. **Do not delete the
   config entry** in Settings → Devices & Services — that entry is what carries
   your history over.
2. Install this integration (below).
3. Restart Home Assistant.

On the next start the existing entry migrates itself. Coming from
`mk-maddin/wattpilot-HA`, the entity registry is rewritten once as well: that
integration keyed unique IDs on the charger's friendly name or IP address,
this one keys them on the serial number, so the entries are renamed in place
and keep their entity IDs, history and customizations.

Both integrations use the same `wattpilot` domain, so **do not run the old and
the new one at the same time.** For side-by-side testing use the development
copy described under [Development](#development), which runs under a separate
domain.

## Features

Every one of the 75 unique IDs `ruaan-deysel/ha-wattpilot` shipped is
reproduced, across seven platforms, plus the additions described below.
The reference device ends up with 80 entities — the rest are gated away by its
firmware and power variant.

| Platform | What it covers |
| --- | --- |
| `sensor` | Charging power, session and total energy, car state and connection, phases in use, charging reason, charger temperature, cable-unlock and lock feedback, Wi-Fi and inverter diagnostics, reboot counters, the ten ID-chip energy counters |
| `switch` | PV surplus charging, charge pause, load balancing, eco mode, battery boost, LED energy saving, NTP, hotspot auto-disable and more |
| `number` | Max charging current, PV-surplus start threshold, minimum charging time, next-trip energy, PV-battery thresholds, phase-switch timings, aWATTar price limit |
| `select` | Charging mode, access control, phase switching, cable unlock, aWATTar country, lock level, boost type, daylight saving, car profile |
| `button` | Start / stop / force charging, restart, authenticate |
| `time` | Next-trip departure |
| `update` | Firmware version, with install |

Diagnostic and noisy entities are registry-disabled by default; enable the ones
you want in the entity settings. Settings you would actually reach for are not
among them: the PV-surplus and phase-switch knobs and the everyday switches
ship enabled, even where the fork left them off.

### New: session energy split

Four extra sensors break the current charging session down by energy source —
**solar**, **battery**, **grid** and **other** (`whs`, `whb`, `whg`, `who`).
These are reverse-engineered from firmware 42.5 and are additions on top of
the fork's entity set, not replacements.

The fifth addition is the **car profile** select (`ct`), which
`mk-maddin/wattpilot-HA` had and `ruaan-deysel/ha-wattpilot` did not.

The sixth is **Phases in use** (`pnp`), the phase count the charger has
switched to. Measured on firmware 42.5, it follows the phase switch rather
than the live current: it moves to 3 roughly 24 seconds after charging starts
and stays there until the car is unplugged.

The last four describe what the charger is doing *right now*, which the
fork's set only lets you infer:

| Entity | Property | What it adds |
| --- | --- | --- |
| **Charging allowed** | `alw` | Whether the car may draw power at this moment — the answer "why is it not charging" usually starts here |
| **Allowed charging current** | `acu` | The current actually offered to the car, as opposed to **Maximum charging current**, which is the configured ceiling. Unknown while nothing is offered |
| **Average charging power** | `tpa` | The charger's own 30-second average, steadier than **Charging power** for threshold automations |
| **Grid frequency** | `fhz` | Diagnostic, **disabled by default**: it changes with nearly every push, so it is recorder load unless you are watching grid quality |

### Energy Dashboard

Use **Total energy** (`sensor.wattpilot_total_energy`, assuming the device is
named "Wattpilot") as the device's consumption source. It is a
`total_increasing` energy sensor and survives charger reboots.

Session energy sensors are *not* suitable as an Energy Dashboard source: they
reset to zero at the start of every session by design.

## Installation

Requires **Home Assistant 2026.8.0** or newer — the version the test suite is
run against on every change, not just the newest one.

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**.
2. Repository `https://github.com/Varitras/ha-wattpilot`, type
   **Integration**.
3. Install **Fronius Wattpilot**, then restart Home Assistant.

### Manual

Copy `custom_components/wattpilot/` into your Home Assistant `config/custom_components/`
directory and restart.

## Configuration

**Settings → Devices & Services → Add Integration → Fronius Wattpilot.**

| Field | Value |
| --- | --- |
| Host | The charger's IP address or hostname on your network |
| Password | The charger password you use in the **Wattpilot.Solar** app |

Give the charger a DHCP reservation or a static address. If it does move, use
**Reconfigure** on the entry rather than deleting and re-adding it — that keeps
the history. If the charger later rejects the stored password, Home Assistant
starts a re-authentication flow on its own.

### Options

**Configure** on the entry offers one setting:

| Option | Default | What it does |
| --- | --- | --- |
| Update interval (seconds) | 5 | Coalesces the charger's pushes: at most one update per entity per interval. `0` forwards every change immediately. See [How data updates](#how-data-updates) — this is not a polling interval. |

Everything is configured through the UI; there is nothing to put in
`configuration.yaml`.

## Actions

| Action | Fields | What it does |
| --- | --- | --- |
| `wattpilot.set_next_trip` | `device_id`, `trigger_time` | Sets the departure time for Next Trip charging mode |
| `wattpilot.set_goe_cloud` | `device_id`, `cloud_api` | Turns the charger's go-e cloud HTTP API on or off |
| `wattpilot.disconnect_charger` | `device_id` | Closes the connection; entities go unavailable |
| `wattpilot.reconnect_charger` | `device_id` | Re-establishes the connection |

All four keep the names and fields of the fork's services, so existing
automations keep working.

### Example

Restrict charging to solar surplus while the sun is up, and set tomorrow's
departure every evening:

```yaml
automation:
  - alias: Wattpilot solar surplus on at sunrise
    triggers:
      - trigger: sun
        event: sunrise
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.wattpilot_pv_surplus_charging

  - alias: Wattpilot solar surplus off at sunset
    triggers:
      - trigger: sun
        event: sunset
    actions:
      - action: switch.turn_off
        target:
          entity_id: switch.wattpilot_pv_surplus_charging

  - alias: Wattpilot next trip at 07:30
    triggers:
      - trigger: time
        at: "21:00:00"
    actions:
      - action: wattpilot.set_next_trip
        data:
          device_id: "{{ device_id('sensor.wattpilot_car_state') }}"
          trigger_time: "07:30:00"
```

## How data updates

The integration holds one WebSocket connection to the charger and subscribes
to property changes. Nothing is polled. When the connection drops, every entity
goes unavailable, the drop is logged once, and the client reconnects on its
own; entities come back as soon as the charger does.

**Update interval** (Settings → Devices & Services → Wattpilot → Configure) is
therefore not a polling interval: it coalesces the charger's pushes. The
charger can push the same property many times a second, and every push writes
entity state and a recorder row. The interval caps that to at most one update
per entity per interval; **0** forwards every change immediately. Availability
is never delayed by it — a push proves the charger is reachable, and that is
signalled at once.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| **Cannot connect to the charger** | Wrong address, charger offline, or a firewall/VLAN between Home Assistant and the charger. Verify with `ping`. |
| **Invalid password** | The password is the **Wattpilot.Solar app** password, not your Wi-Fi or Fronius account password. |
| **The charger at this address has a different serial number** | The address now belongs to another charger. Use **Reconfigure** on the right entry. |
| **This entry belongs to charger X, but the charger at this address reports serial Y** | Setup refuses rather than silently moving this entry's history onto another device. If the address was reused, point the entry at the right one with **Reconfigure**. If you replaced the charger, delete the entry and add the new one — the serial is the entry's identity, so the old history cannot follow. |
| **All entities unavailable** | The charger is offline or the connection dropped. It reconnects by itself — no restart needed. |
| **Cloud connections are not supported yet** | An entry migrated from an older integration was configured for cloud. Reconfigure it with the local address. |
| **Some entities are missing** | They are either firmware- or variant-gated (not offered by your charger), or disabled by default. Check the disabled entities on the device page. |

For a bug report, attach the entry's **Download diagnostics** output. It is
redacted before it reaches you: serial number, host address, passwords, cloud
API tokens, MAC addresses, IP addresses, Wi-Fi SSIDs and the log of
neighbouring networks are dropped or replaced.

## Removing the integration

Settings → Devices & Services → **Fronius Wattpilot** → ⋮ → **Delete**. That
removes the entry, its device and all its entities. If you installed via HACS,
remove it there too and restart. Nothing is left behind on the charger.

## Development

```bash
bash scripts/check.sh
```

runs every gate: `ruff format --check`, `ruff check`, `mypy --strict` and the
test suite. `bash scripts/check.sh --release` adds the end-to-end tests and the
mutation-testing gate.

The test suite needs Linux (`pytest-homeassistant-custom-component` does not
run on Windows); on Windows use WSL2. Python 3.14 is required.

To try a change against a live charger while the old integration keeps running
in production:

```bash
python scripts/make_dev_copy.py
```

which produces a copy of the integration under a separate domain, so both can
be installed at the same time.

## Credits

- [joscha82/wattpilot](https://github.com/joscha82/wattpilot) — original
  protocol reverse engineering.
- [mk-maddin/wattpilot-HA](https://github.com/mk-maddin/wattpilot-HA) — the
  original Home Assistant integration concept.
- [ruaan-deysel/ha-wattpilot](https://github.com/ruaan-deysel/ha-wattpilot) — the
  integration this project replaces.
- [`wattpilot-api`](https://github.com/ruaan-deysel/wattpilot-api) — the async
  client library this project started from. Version 1.4.0 was adopted into
  `custom_components/wattpilot/api/` and is maintained here.

See [NOTICE](NOTICE) for the licensing details.

This project is **not affiliated with or endorsed by Fronius International
GmbH**. "Fronius" and "Wattpilot" are trademarks of their respective owners.
Licensed under the [MIT License](LICENSE).
