# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Entities can no longer go missing when the integration connects. The
  charger sends its first status in several pieces, and a live update
  arriving between two of them used to count as "all properties known" --
  whatever came later got no entity until the integration was reloaded.
  Possible rather than observed: after a restart the pieces paused for
  about a second, while live updates arrive once a second.
- The restart button no longer reports a failure on every press. The
  charger restarts without ever answering the command, and waiting for that
  answer ran into a timeout each time.

## [0.1.2] - 2026-09-19

Everything from the 0.1.2b1 candidate, plus what an independent audit of it
found. Nothing here adds functionality; it is quieter in the log and holds up
better at a few edges.

### Changed

- A charger that stops answering is reported once, at INFO instead of WARNING.
  Switching the charger off is not a fault you can act on, and Home Assistant
  shows everything from WARNING up in the log panel, attributed to this
  integration. The reconnect attempts behind it repeated their own warning for
  as long as the charger stayed away; they are debug-only now.

### Fixed

- Two reconnect actions arriving at the same time no longer leave a stray
  connection behind. Each opened its own socket, and only the last one was
  ever closed again.
- The "enable cloud API" action gives up when its timeout says so. It used
  to count whole seconds, so a short timeout still waited a full second.
- A diagnostics download no longer fails on a `cards` value that is not a
  list, and no longer walks a string there character by character. Not seen
  from real firmware; the same guard the companion-device field already had.
- An empty `nrg` array no longer stops the reader. It raised an error the
  frame guard did not catch, and the valid frame behind it was lost while the
  connection still looked healthy. Not observed on real firmware.

### Removed

- The unused half of the adopted client: child-property expansion, an MQTT
  bridge configuration and a discovery configuration that nothing here ever
  called. No user-visible change.

## [0.1.2b1] - 2026-09-09

A release candidate. It changes what the integration writes into your log,
and hardens the reader against one malformed frame nobody has seen a charger
send. Install it if your log panel is full of warnings from this integration
-- otherwise there is no reason to hurry.

### Changed

- A charger that stops answering is reported once, at INFO instead of WARNING.
  Switching the charger off is not a fault you can act on, and Home Assistant
  shows everything from WARNING up in the log panel, attributed to this
  integration. The reconnect attempts behind it repeated their own warning for
  as long as the charger stayed away; they are debug-only now.

### Fixed

- An empty `nrg` array no longer stops the reader. It raised an error the
  frame guard did not catch, and the valid frame behind it was lost while the
  connection still looked healthy. Not observed on real firmware.

## [0.1.1] - 2026-09-06

A maintenance release. Everything below came out of an independent audit of
0.1.0; there is no new functionality.

### Fixed

- The departure time for Next Trip is sent unchanged. It was shifted an hour
  forward during summer time, and read back without that shift -- so a trip
  set to 07:30 showed up as 08:30. Measured against the charger: it expects
  plain local time, and applies no daylight-saving correction of its own.
  **If you set a departure time with 0.1.0 during summer time, check it.**
- Re-entering the password now verifies which charger answered. If the
  address had been taken over by another one, the new password was saved and
  the flow reported success, while the entry itself then refused to start.
- An ID-chip sensor for a slot the charger does not have shows nothing
  instead of writing the whole card list, holder names included, into the
  log. That log is not covered by the redaction that protects diagnostics.
- Reconnecting by action while the integration was already retrying no longer
  leaves the earlier attempt running, with two readers on one connection.
- An automatic reconnect gives up on a charger that accepts the connection
  and then never finishes talking, instead of waiting on it forever.
- Nothing from a charger that was refused as the wrong one is applied any
  more: what its connection had already sent used to be processed after the
  refusal.
- A setup that Home Assistant cancels hands the connection back in every
  phase, including while the client is still reading its protocol
  description.
- The firmware update honours its own time limit. The limit counted only the
  waiting between attempts, so an update could run far past it.

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
  it, and it no longer pulls in the MQTT, shell and pydantic dependencies it
  had: the client needs `websockets` and `bcrypt` (plus PyYAML, which Home
  Assistant ships), and the manifest declares those two and `packaging`.

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

[Unreleased]: https://github.com/Varitras/ha-wattpilot/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/Varitras/ha-wattpilot/compare/v0.1.2b1...v0.1.2
[0.1.2b1]: https://github.com/Varitras/ha-wattpilot/compare/v0.1.1...v0.1.2b1
[0.1.1]: https://github.com/Varitras/ha-wattpilot/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Varitras/ha-wattpilot/releases/tag/v0.1.0
