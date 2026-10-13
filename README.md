# Fronius Wattpilot for Home Assistant

[![Release](https://img.shields.io/github/v/release/Varitras/ha-wattpilot?sort=semver)](https://github.com/Varitras/ha-wattpilot/releases)
[![CI](https://github.com/Varitras/ha-wattpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Varitras/ha-wattpilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![HACS: custom](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz)

A local-push Home Assistant integration for **Fronius Wattpilot** wallbox
chargers. It talks to the charger over its WebSocket API on your own network —
no cloud account, no polling. When the charger pushes a property, exactly
the entities that use it update.

It is a **drop-in replacement** for the two existing community integrations,
[`ruaan-deysel/ha-wattpilot`][deysel] and [`mk-maddin/wattpilot-HA`][maddin]:
same `wattpilot` domain, the entity unique IDs of `ruaan-deysel/ha-wattpilot`
(those of `mk-maddin/wattpilot-HA` are rewritten once, see below), automatic
config-entry migration. Your history, dashboards, automations and entity
customizations survive the switch.

[deysel]: https://github.com/ruaan-deysel/ha-wattpilot
[maddin]: https://github.com/mk-maddin/wattpilot-HA

## Supported devices

Fronius Wattpilot chargers reachable on the local network, in both the 11 kW
and 22 kW variants. Developed and verified against a **Wattpilot Home 11 J 2.0
on firmware 42.5**; the Go models speak the same API but have not been tested
here. Support for the **Wattpilot Flex** rests on diagnostics a Flex owner
shared (firmware 43.4 and 43.5); it has not been tested here either. Older
firmware is supported: entities that a given firmware or power
variant does not provide are simply not created, so no entity ever sits at a
permanently unknown state.

**Not supported yet:** cloud connections. A cloud-configured entry migrated
from an older integration will fail setup with a clear message asking you to
reconfigure it with the charger's local address.

## Migrating from ha-wattpilot or wattpilot-HA

1. Remove the old integration in HACS: open it there and choose ⋮ →
   **Remove**. **Do not delete the config entry** in Settings → Devices &
   Services — that entry is what carries your history over.
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
reproduced, plus the additions described below, across eight platforms.
The reference device ends up with 111 entities — the rest are gated away by its
firmware and power variant.

| Platform | What it covers |
| --- | --- |
| `sensor` | Charging power, voltage, current, power and power factor per phase, session and total energy, car state and connection, phases in use, charging reason, charger temperature, cable-unlock and lock feedback, Wi-Fi and inverter diagnostics, reboot counters, the ten ID-chip energy counters, electricity price, charging duration and more |
| `binary_sensor` | Car plugged in, car charging |
| `switch` | PV surplus charging, charge pause, load balancing, eco mode, battery boost, LED energy saving, NTP, hotspot auto-disable and more |
| `number` | Max charging current, PV-surplus start threshold, minimum charging time, next-trip energy, PV-battery thresholds, phase-switch timings, aWATTar price limit, energy limit per charge, car consumption, charge-pause, charging-interval, single-phase and simulated-unplugging durations and more |
| `select` | Charging mode, access control, phase switching, cable lock behavior, aWATTar country, button current change lock, boost type, daylight saving, car profile, forced charging state, charging current preset |
| `button` | Start / stop / force charging, restart, authenticate |
| `time` | Next-trip departure |
| `update` | Firmware version, with install |

Noisy entities and most diagnostic ones are registry-disabled by default;
enable the ones you want in the entity settings. Settings you would actually
reach for are not among them: the PV-surplus knobs, the phase-switch delay and
interval and the everyday switches ship enabled, even where the fork left them
off. The three-phase power level stays disabled, as in the fork.

### Additions to the fork's entity set

Four extra sensors break the current charging session down by energy source —
**solar**, **battery**, **grid** and **other** (`whs`, `whb`, `whg`, `who`).
These are reverse-engineered from firmware 42.5 and are additions on top of
the fork's entity set, not replacements.

The fifth addition is the **car profile** select (`ct`), which
`mk-maddin/wattpilot-HA` had and `ruaan-deysel/ha-wattpilot` did not.

The sixth is **Phases in use** (`pha`), the number of phases switched
through to the car: 1 or 3 while charging, 0 when no phase is switched. It
changes together with the charger's contactor, so at a start it can read 3
a few seconds before current flows. The setting behind it, automatic or a
fixed phase count, is the **Phase switching** select.

These describe what the charger is doing *right now*, which the fork's set
only lets you infer:

| Entity | Property | What it adds |
| --- | --- | --- |
| **Charging allowed** | `alw` | Whether the car may draw power at this moment — the answer "why is it not charging" usually starts here |
| **Allowed charging current** | `acu` | The current actually offered to the car, as opposed to **Maximum charging current**, which is the configured ceiling. Unknown while nothing is offered |
| **Average charging power** | `tpa` | The charger's own 30-second average, steadier than **Charging power** for threshold automations. Reads 0 W once no current has flowed for a minute, because the charger itself keeps the last session's average; a shorter pause keeps the value, meant to bridge a phase switch, whose length has not been measured. A new session reads 0 W until the charger reports its own average |
| **Grid frequency** | `fhz` | Diagnostic, **disabled by default**: it changes in more than half of all pushes, so it is recorder load unless you are watching grid quality |
| **L1/L2/L3/N voltage**, **current**, **power**, **power factor** | `nrg` | One sensor per phase and reading, with history (no current on N). **Disabled by default**: they change with nearly every push. The same values stay attributes of **Charging power**, without history |
| **Charging power from solar**, **battery**, **grid**, **other sources** | `ws`, `wb`, `wg`, `wo` | The live counterpart of the session energy split. Reported by a Wattpilot Flex on firmware 43.x and created only on a charger that reports them. The unit is taken to be watts by analogy with the energy split; it has not been measured. **Disabled by default** |
| **Charging current limit** | `cll` | The lowest of the charger's hardware limits. The single limits are attributes: adapter, cable, temperature, phase unbalance, the requested current, and `pvSurplus`, the current PV surplus charging offers while it runs. Diagnostic, **disabled by default** |
| **Temperature current limit** | `amt` | The temperature limit on its own. Diagnostic, **disabled by default** |

And a few that make existing settings usable from Home Assistant:

| Entity | Property | What it adds |
| --- | --- | --- |
| **Car plugged in**, **Car charging** | `car` | Yes/no versions of the car state, for automations and dashboards. Unknown while the charger reports an unknown or error state |
| **Forced charging state** | `frc` | The state the three charging buttons set (Neutral, Off, On), now readable. It does not survive a power cut: the charger comes back as Neutral |
| **Charging current preset** | `amp`, `clp` | Sets the current to one of the presets configured in the app. The app's current slider stops only at those presets, so a current in between (13 A with presets 12 and 14) has no position there; this select shows no option for it either |
| **Energy limit per charge** | `dwo` | Stops the charge after this much energy. **0 means no limit** |
| **Electricity price** | `awcp`, `awpl` | The current market price of a dynamic tariff in ct/kWh, with the charger's price list as the `prices` attribute. **Disabled by default** |
| **Car consumption** | `cco` | kWh per 100 km, which the app uses to show range. **Disabled by default** |
| **Charging duration** | `cdi`, `rbt` | Minutes of the current charge, or of the last one while no car charges |
| **Minimum charge pause**, **Minimum charging interval**, **Forced single-phase duration**, **Simulated unplugging duration** | `mcpd`, `mci`, `psmd`, `sumd` | Timings from the app, in seconds. 0 switches the first two off. **Disabled by default** |
| **PV surplus stop hysteresis** | `sh` | The stop hysteresis of PV surplus charging in W, as the app sets it; 200 W on the chargers seen |
| **Minimum charging current** | `mca` | The lowest current the charger offers, from 6 A up to the variant's maximum. **Disabled by default** |

### Energy Dashboard

Use **Total energy** (`sensor.wattpilot_total_energy`; on an installation set
up with 0.2.0 or earlier `sensor.wattpilot_<serial>_total_energy`) as the
device's consumption source. It is a
`total_increasing` energy sensor and survives charger reboots, also across a
reload or restart of Home Assistant.

The session energy sensors add nothing there: they start again at zero with
every session, and Total energy already counts the same energy.

## Installation

Requires **Home Assistant 2026.9.0** or newer — the version the test suite is
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

Home Assistant finds the charger on your network by itself (mDNS) and offers
it under **Settings → Devices & Services** as discovered; only the password is
left to enter. To add it by hand instead:

**Settings → Devices & Services → Add Integration → Fronius Wattpilot.**

| Field | Value |
| --- | --- |
| Host | The charger's IP address or hostname on your network |
| Password | The charger password you use in the **Wattpilot.Solar** app |

A charger that gets a new address is followed on its own: when the entry has
lost its charger and the charger announces itself at a new address, the entry
takes it and reloads. An entry that is still connected ignores announcements
elsewhere, an entry set up by name keeps the name, and an entry whose setup
failed takes a new address and reloads as well. A DHCP reservation
still does no harm. If it moved without announcing itself, use
**Reconfigure** on the entry rather than deleting and re-adding it — that
keeps the history. If the charger later rejects the stored password, Home Assistant
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

Use PV surplus charging while the sun is up, and set tomorrow's
departure every evening, with the entity IDs of a new installation:

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
to property changes. Nothing is polled. When the connection drops and is still
down at the next check (every 30 seconds), every entity goes unavailable, the
drop is logged once, and the client reconnects on its own, at growing
intervals of up to five minutes; entities come back as soon as the charger has
sent its current readings. A reconnect between two
checks goes unnoticed.

**Update interval** (Settings → Devices & Services → Fronius Wattpilot →
Configure) is therefore not a polling interval: it coalesces the charger's
pushes. The
charger can push the same property many times a second, and every push writes
entity state and a recorder row. The interval caps that to at most one update
per entity per interval; **0** forwards every change immediately. Availability
is never delayed by it: a recovery is signalled with the first push after the
charger's readings are in, together with those readings.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| **Cannot connect to the charger** | Wrong address, charger offline, or a firewall/VLAN between Home Assistant and the charger. Verify with `ping`. |
| **Invalid password** | The password is the **Wattpilot.Solar app** password, not your Wi-Fi or Fronius account password. |
| **The charger at this address has a different serial number** | The address now belongs to another charger. Use **Reconfigure** on the right entry. |
| **This entry belongs to charger X, but the charger at this address reports serial Y** | Setup refuses rather than silently moving this entry's history onto another device. If the address was reused, point the entry at the right one with **Reconfigure**. If you replaced the charger, delete the entry and add the new one — the serial is the entry's identity, so the old history cannot follow. |
| **Charger X is already configured by another entry** or, in Reconfigure, **This charger already has another entry** | An entry carried over from the earlier integration found its charger already set up a second time, from a discovery card or by hand. Delete that other entry, then reload the carried-over one, or submit Reconfigure or the re-authentication again if that is where the message came up: only the carried-over entry keeps the earlier integration's entity IDs and history. |
| **Repair notice: Another charger answers at this charger's address** | The situation of *This entry belongs to charger X*, found while running: the integration stops connecting instead of showing the other charger's readings. Point the entry at the right address with **Reconfigure**; the notice goes away with the reload that follows. |
| **Repair notice: The charger refused the connection** | The charger asks for a sign-in method this version of the integration does not know, usually after a firmware update. Look for an update of the integration; the notice goes away once the charger is connected again or the integration is reloaded. |
| **All entities unavailable** | The charger is offline or the connection dropped. It reconnects by itself, within five minutes of the charger being reachable again — no restart needed. |
| **Cloud connections are not supported yet** | An entry migrated from an older integration was configured for cloud. Reconfigure it with the local address. |
| **Some entities are missing** | They are either firmware- or variant-gated (not offered by your charger), or disabled by default. Check the disabled entities on the device page. |

For a bug report, attach the entry's **Download diagnostics** output. It is
redacted before it reaches you: serial number, host names, the device name
the entry stores, passwords, cloud
API tokens, MAC addresses, Wi-Fi SSIDs, ID-chip names, the log of
neighbouring networks and free-text fields such as a load group name, a
Wi-Fi error message, the last cloud error or a Wattpilot Flex's OCPP
fallback card and tariff text are dropped or replaced. MAC
and IP addresses and the serial number are also replaced wherever they
appear inside other text. Still look over the file before posting it.

The log is masked as far as this integration reaches: its own messages and
Home Assistant's messages about setting up the entry show only the last two
digits of the serial number, and its own messages name an entity by its key.
On an installation set up with 0.2.0 or earlier, entity IDs contain the serial
number -- the device is named after its host name, `Wattpilot_<serial>` -- and
other parts of Home Assistant log entity IDs, recorder warnings for example.
Look over a log before posting it.

## Removing the integration

Settings → Devices & Services → **Fronius Wattpilot** → ⋮ → **Delete**. That
removes the entry, its device and all its entities. If you installed via HACS,
remove it there too and restart. The integration stores nothing on the
charger, but settings changed through it stay; if you turned on the go-e cloud
API with `wattpilot.set_goe_cloud`, turn it off first.

## Development

```bash
bash scripts/check.sh
```

runs every gate: `ruff format --check`, `ruff check`, `mypy --strict`,
`shellcheck` and the full test suite, end-to-end tests included. A gate whose
tool is missing fails the run; `pip install --group dev` installs them all.
`bash scripts/check.sh --release` adds the mutation-testing gate on top.

The test suite needs Linux (`pytest-homeassistant-custom-component` does not
run on Windows); on Windows use WSL2. Python 3.14 is required.

To try a change against a live charger while the old integration keeps running
in production:

```bash
python scripts/make_dev_copy.py <config>/custom_components
```

which copies the integration there under a separate domain, `wattpilot_dev`,
so both can be installed at the same time. Without a target it writes into this
repository's own `custom_components/`.

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
