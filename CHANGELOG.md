# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Everything below is still part of the unreleased 0.1.0; it is listed
separately because it came out of a full audit of the branch rather than
from building the integration.

### Added

- The Wattpilot client is part of this project now
  (`custom_components/wattpilot/api/`), adopted from `wattpilot-api` 1.4.0
  (MIT) after upstream went quiet. Three defects could only be fixed inside
  it, and its dependencies shrank from six to one in the process.
- A repair issue for entities a migration had to skip because their target
  identifier was already taken. Nothing is deleted; the issue names the
  entities and explains both ways out.
- The charger's firmware version is watched: a change reloads the entry, so
  newly supported entities appear and the device page stops showing the old
  version.

### Fixed

- Writes are acknowledged. A value the charger refuses now fails the action
  instead of reporting success.
- Credentials no longer reach the logs. The client logged every frame
  verbatim on DEBUG, including Wi-Fi passwords, cloud tokens and OCPP keys.
- A failure during setup hands back the connection, callback and timers
  instead of leaving them running.
- A disconnect that fails still marks the charger unavailable, rather than
  leaving entities claiming a charger that can no longer reach them.
- Diagnostics replace the configured time server, which can be an internal
  hostname or a private address, and survive payload shapes they do not
  know instead of failing the download.
- Enabling the go-e cloud API no longer logs fragments of the key or the
  URL carrying the serial number.
- `trx` starts at its real value: `null` is that property's "no
  transaction", not the absence of a value.

### Changed

- Ten entities ship enabled that were off before: the four session-energy
  sources, the phase-switch delay and interval, the PV-battery discharge
  floor, and the switches for charge pause, PV-battery discharge and
  simulated unplugging. They are settings and readings people look for, not
  diagnostics -- six of them are enabled here where the fork disables them.
- The aWATTar price limit drops its monetary device class. It is stored in
  cent, and Home Assistant expects an ISO 4217 currency code there.
- The full-setup test runs on every change instead of only at release.

## [0.1.0] - unreleased

### Added

- Initial release: local-push integration for Fronius Wattpilot chargers,
  covering 80 entities across sensor, switch, number, select, button, time and
  update.
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

### Changed (relative to the fork)

- "Car connection" reports Connected/Disconnected instead of repeating the
  charge state, which "Car state" already shows. Both entities read the same
  property, so mapping both through the charge-state labels made them
  duplicates.

### Fixed (relative to the fork)

- Session energy resets are no longer suppressed by a monotonicity guard, so a
  new charging session starts at zero instead of holding the previous total.
- The config entry's unique ID is the charger serial rather than its IP
  address, so a DHCP change no longer orphans the entry.
- A property push updates only the entities that use that property, instead of
  fanning out over every entity through a coordinator.
- Entities whose property the charger's firmware or power variant does not
  provide are no longer created, instead of existing permanently unknown.
- The client's API definition file is loaded in an executor during setup rather
  than blocking the event loop on the first write.

[Unreleased]: https://github.com/Varitras/ha-wattpilot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Varitras/ha-wattpilot/releases/tag/v0.1.0
