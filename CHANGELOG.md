# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-05

First release. There is no earlier version of this project, so the
**Changed** and **Fixed** entries below are relative to what they replace:
the two community integrations [`ruaan-deysel/ha-wattpilot`][deysel] and
[`mk-maddin/wattpilot-HA`][maddin], and the `wattpilot-api` client the
former builds on.

[deysel]: https://github.com/ruaan-deysel/ha-wattpilot
[maddin]: https://github.com/mk-maddin/wattpilot-HA

### Added

- Local-push integration for Fronius Wattpilot chargers, covering 80 entities
  across sensor, switch, number, select, button, time and update.
- Drop-in replacement for `ruaan-deysel/ha-wattpilot`: config entries migrate
  automatically and entity unique IDs are unchanged, so history, dashboards and
  automations survive the switch.
- One-time entity-registry migration for `mk-maddin/wattpilot-HA` installs,
  which keyed unique IDs on the friendly name or IP address instead of the
  serial.
- Session energy split sensors (solar, battery, grid, other).
- "Phases in use" sensor, reporting the phase count the charger has switched
  to (`pnp`). Created only on chargers that report the property.
- "Charging allowed" (`alw`) and "Allowed charging current" (`acu`): what the
  charger is offering the car right now, as opposed to the configured limit.
- "Average charging power" (`tpa`), the charger's own 30-second average.
- "Grid frequency" (`fhz`), diagnostic and disabled by default.
- Reauthentication and reconfiguration flows, redacted diagnostics, and German
  and English translations for entities, actions and errors.
- The charger's firmware version is watched: a change reloads the entry, so
  newly supported entities appear and the device page stops showing the old
  version.
- A repair issue for entities a migration had to skip because their target
  identifier was already taken. Nothing is deleted; the issue names the
  entities and explains both ways out.
- The Wattpilot client is part of this project
  (`custom_components/wattpilot/api/`), adopted from `wattpilot-api` 1.4.0
  (MIT) after upstream went quiet. Several defects could only be fixed inside
  it, and its dependencies shrank from six to one in the process.

### Changed

- "Car connection" reports Connected/Disconnected instead of repeating the
  charge state, which "Car state" already shows. Both entities read the same
  property, so mapping both through the charge-state labels made them
  duplicates.
- Ten entities ship enabled that the fork leaves off: the four session-energy
  sources, the phase-switch delay and interval, the PV-battery discharge
  floor, and the switches for charge pause, PV-battery discharge and
  simulated unplugging. They are settings and readings people look for, not
  diagnostics.
- The aWATTar price limit drops its monetary device class. It is stored in
  cent, and Home Assistant expects an ISO 4217 currency code there.

### Fixed

- Session energy resets are no longer suppressed by a monotonicity guard, so a
  new charging session starts at zero instead of holding the previous total.
- The config entry's unique ID is the charger serial rather than its IP
  address, so a DHCP change no longer orphans the entry.
- A property push updates only the entities that use that property, instead of
  fanning out over every entity through a coordinator.
- Entities whose property the charger's firmware or power variant does not
  provide are no longer created, instead of existing permanently unknown.
- Writes are acknowledged. A value the charger refuses fails the action
  instead of reporting success.
- Credentials no longer reach the logs. The client logged every frame
  verbatim on DEBUG, including Wi-Fi passwords, cloud tokens and OCPP keys.
- Enabling the go-e cloud API no longer logs fragments of the key or the
  URL carrying the serial number.
- Diagnostics replace the configured time server, which can be an internal
  hostname or a private address, and survive payload shapes they do not
  know instead of failing the download.
- Diagnostics redact the nested data too. Addresses, Wi-Fi names and MAC
  addresses inside `dns`, `wifis` and `scan` reached the download because
  the client built them in a shape the redaction did not walk. The hostname
  fields carrying the serial number are replaced as well.
- A reconnect checks that the charger which answered is the one the entry
  belongs to. The address can be reused or the hardware replaced, and the
  entry, its entities and their history stayed pointed at whatever picked up.
- A reconnect waits for the charger's new snapshot instead of accepting the
  first partial one and reporting itself ready.
- One unreadable frame no longer silences the charger: it is logged and
  skipped rather than ending the connection while it still looks alive.
- A failure during setup hands back the connection, callback and timers
  instead of leaving them running, and a setup Home Assistant cancels does
  the same.
- A disconnect that fails still marks the charger unavailable, rather than
  leaving entities claiming a charger that can no longer reach them.
- The client's API definition file is loaded in a thread during setup rather
  than blocking the event loop on the first write, and so is the password
  hashing of every connection.
- `trx` starts at its real value: `null` is that property's "no
  transaction", not the absence of a value.

[Unreleased]: https://github.com/Varitras/ha-wattpilot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Varitras/ha-wattpilot/releases/tag/v0.1.0
