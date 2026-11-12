# Release notes

## 0.3.0

Requires Home Assistant **2026.11.0+** and HACS **2.0.5+**. Restart Home Assistant
and refresh the browser after updating.

- Configure roller shutter V3 travel times, reverse position, motor startup
  compensation and optional slat control from one settings dialog.
- Calibrate opening/closing and slat times with a stable timer, separate measured
  and editable results, optional preservation of total times, and slat-angle
  testing. Cancelling a measurement leaves the module's movement running.
- Add native CLU status/log-level diagnostics, uptime-based restart detection,
  opt-in diagnostic file capture and Telnet log forwarding.
- Add standalone CLI commands for diagnostics, firmware inventory, native logs,
  attribute/variable inspection and bounded file downloads.
- Update Home Assistant APIs for device metadata, entity renaming and thermostat
  temperature readings; fix Polish/English shutter menu titles and labels.
- Bound interface downloads and JSON decompression, suppress PIN-bearing HTTP
  exception tracebacks, and save CLI encryption keys atomically with owner-only
  permissions.
- Simplify the README and move protocol details and automation examples into the
  [device and protocol reference](docs/reference.md).

Shutter timing settings are written to the CLU. Slat movement time is shared by
both directions; compensating the opposite direction is optional and starts off.

## 0.2.0

Requires Home Assistant **2026.9.4+** and HACS **2.0.5+**.

- Add `THERMOSTAT_V2` climate entities with heating, optional cooling, and
  manual/schedule mode independent of control direction.
- Manage weekly schedules with day sections, 15-minute periods, 0.5°C targets,
  a schedule sensor and configurable automatic synchronization.
- Fix schedule decoding, preserve unchanged encoded values and verify schedule
  writes through readback.
- Add minimum/maximum temperature sensors and configurable thermostat limits.
- Configure polling intervals and initial subscription IDs per CLU. Move
  variable management into CLU configuration and support per-variable read modes.
- Reduce realtime traffic by polling temperatures and diagnostic values with
  `fetchValues`, and reading large attributes with isolated `OBJECT:get(index)`
  requests.
- Refactor subscriptions, shared report listeners, renewal timing and shutdown
  cleanup based on Object Manager traffic analysis.
- Improve availability handling and timeout logs with CLU, subscription and
  target-key context.
- Improve dimmer actions, ON/OFF light detection by name/icon, entity filtering,
  translations and sensor units without a device class.
- Simplify Grenton source/action attributes, expand device documentation and
  add regression coverage.

Thanks to [@sszczep](https://github.com/sszczep) for creating the original integration.

[Full changelog](https://github.com/k911/homeassistant-grenton/compare/0.1.5...0.2.0)
