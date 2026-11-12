# Home Assistant Grenton

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/k911/homeassistant-grenton?style=for-the-badge)](https://github.com/k911/homeassistant-grenton/releases)

Control Grenton lights, switches, shutters, thermostats and other devices in Home Assistant. Import a mobile interface from Grenton Object Manager, then customize entities through the integration's configuration dialogs. The interface supplies device bindings; Home Assistant communicates with the CLUs for control and state updates.

![Grenton Integration](assets/dashboard.png)

## Requirements

- **Home Assistant 2026.11.0 or newer**; tested with **2026.11.1**.
- **HACS 2.0.5 or newer** for installation through HACS.
- A Grenton mobile interface available in Object Manager, its PIN, and network access from Home Assistant to Object Manager and the CLUs.

## Installation

### HACS

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/k911/homeassistant-grenton` with category **Integration**.
3. Find **Grenton**, download it, and restart Home Assistant.

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=k911&repository=homeassistant-grenton&category=integration)

When switching from another Grenton fork, keep only one Grenton repository in HACS and retain the existing entry under **Settings → Devices & services**. This integration uses the `grenton` domain.

### Manual installation

1. Download and extract the [latest release](https://github.com/k911/homeassistant-grenton/releases).
2. Copy the contents of `custom_components/homeassistant_grenton/` into `/config/custom_components/grenton/`.
3. Restart Home Assistant.

HACS and manual installation use the same destination folder. After updating integration files, restart Home Assistant and refresh the browser to load updated configuration controls.

## Setup

1. Open **Settings → Devices & services → Add integration** and search for **Grenton**.
2. Enter the Object Manager IP address, port (default **9998**), and mobile-interface PIN.
3. Confirm the import. Supported widgets and CLU controllers appear as Home Assistant devices.

If you add devices or change bindings in Object Manager later, open the Grenton integration's **⋮ → Reconfigure** menu to import the interface again. Existing entity options are retained.

## Supported widgets

| Grenton widget | Home Assistant entity |
| --- | --- |
| `VALUE_V2` | Sensor or binary sensor |
| `VALUE_DOUBLE` | Two independently configurable sensors or binary sensors |
| `ON_OFF`, `ON_OFF_DOUBLE` | Switches or on/off lights |
| `DIMMER_V2` | Dimmable light |
| `LED` | Light, with brightness and color where supplied |
| `CONTACT_SENSOR`, `CONTACT_SENSOR_DOUBLE` | One or two binary sensors |
| `SLIDER` | Number control |
| `THERMOSTAT_V2` | Thermostat with optional cooling and weekly schedules |
| `MULTISENSOR` | Multiple sensors |
| `ROLLER_SHUTTER_V3` | Shutter/blind cover with optional slat control and calibration |
| `ROLLER_SHUTTER` | Legacy state sensor and action button |
| `CAMERA` | Camera |
| `SCENE` | Configurable action button |

Supported CLUs also expose controller status and diagnostic entities, including uptime, connection status, firmware version and cloud settings. Available diagnostics depend on the controller model.

## Configure entities

Open **Settings → Devices & services → Grenton → Configure → Configure entities**. Select an entity, optionally filtering by type.

- **Sensors and numbers:** choose a device class, unit and, for numbers, display mode. Choose **None** for no device class or unit; custom units and `%` are also available.
- **Value widgets:** choose Sensor or Binary sensor. For binary sensors, positive values mean off by default; **Invert state** reverses the mapping. Missing values remain unknown.
- **On/off controls:** choose Switch or Light. Imported light names/icons are detected automatically; a saved choice takes precedence.
- **Dimmers and scenes:** configure the state source or action when the imported bindings need adjustment.
- **Thermostats and shutters:** use the controls described below.

Changing an entity between domains, such as Switch and Light, changes its entity ID. Update affected dashboards and automations. Devices can be renamed through Home Assistant's device settings; custom names survive reloads and interface imports.

### Shutters and blinds

Select a `ROLLER_SHUTTER_V3` entity, then choose **Shutter settings** or **Opening and closing time calibration**.

Settings include opening/closing travel times, motor startup compensation, reverse position and optional slat control. Enabling slat control reveals its movement-time field. Disabling it saves the slat time as **0 ms**. Home Assistant displays 0% as closed and 100% as open.

To calibrate travel times:

1. Choose opening or closing, review that direction's movement time, and configure slat support if needed.
2. Make sure the shutter is fully at the opposite end. The positioning button moves it in the appropriate direction; wait for the module to finish before continuing.
3. Start measurement. Without slats, finish when the shutter reaches the end. With slats, mark both stages: opening measures slat rotation first, then travel; closing measures travel first, then slat rotation.
4. Review and save the result. The summary preserves the exact measured times and shows the currently configured times. Editable suggestions round up to 100 ms.

**Keep total time unchanged** links travel and slat adjustments. The slat time is shared by both directions; an additional checkbox, off by default, adjusts the opposite travel time to preserve that direction's total runtime. After a slat calibration, test angles from **0° (open)** to **90° (closed)** and adjust the timings if necessary.

**Finishing measurement or cancelling the wizard does not stop the motor's calibration movement.** The module completes that movement independently. A result can be saved while calibration finishes; slat testing waits until it is complete.

### Thermostats

`THERMOSTAT_V2` supports **Heat**, optional **Cool**, **Auto** (weekly schedule), and **Off**. The schedule runs on the CLU and continues while Home Assistant is offline. Choose Heat or Cool to edit the manual target; Auto follows the saved schedule.

Select the thermostat under **Configure entities** to enable cooling, change control direction, edit temperature limits or edit its weekly schedule. Changing direction in Auto preserves schedule mode.

The schedule editor groups days with the same settings. Each day starts at midnight; further periods use 15-minute boundaries and temperatures in 0.5°C increments. Saving replaces the complete week. The **Schedule** sensor on the thermostat's device page shows the current scheduled target and next change.

**Automatically sync schedule** is enabled by default and refreshes the schedule while the thermostat is on in Auto. Turn it off to read schedules only when opening configuration or saving. Home Assistant's time zone should match the CLU clock.

### Controller settings and variables

Under **Configure → Configure CLU**, select a controller:

- **Connection and polling:** set its polling interval (default **60 seconds**). Temperature readings use polling; other entities use realtime updates where the controller supports them. HTTP Gate uses polling. Shorter intervals increase network traffic.
- **Variables:** expose an existing Grenton variable as a switch or sensor, then configure its display name, type and read mode. Removing its Home Assistant entity leaves the variable on the CLU intact.

Leave **Initial subscription ID** at its default unless troubleshooting a subscription conflict. Each controller has its own polling and subscription settings.

### Controller diagnostics and logs

Open a CLU's device page to inspect **Connection**, **Request timeouts**, **Uptime**, **Last restart**, and other supported diagnostics. A detected restart identifies an uptime reset; it does not identify the cause. Restart tracking establishes a new baseline whenever the integration reloads.

For troubleshooting, supported controllers provide these optional switches:

| Switch | Effect |
| --- | --- |
| **Debug mode** | Enables native DEBUG logging; turning it off disables native logging. |
| **Debug log capture** | Enables DEBUG logging and streams CLU output into Home Assistant's full logs. |
| **Diagnostic file capture** | Attempts to save a diagnostic file after a detected restart. |

Only one Telnet client can listen to a CLU at a time. Close Object Manager's or the CLI's log listener before enabling **Debug log capture**. Capture starts off after every Home Assistant restart or integration reload. Turning it off closes the stream but leaves native DEBUG enabled; turn off **Debug mode** separately when finished.

Diagnostic file capture defaults to off. It retains up to five files per controller under `/config/grenton/debug/`; the **Last restart** sensor shows the saved file path or capture error. Files are limited to 4 MiB each. Some restarts produce no diagnostic file.

## Troubleshooting

- **Cannot connect:** check Object Manager's address, port and PIN, and connectivity from Home Assistant to the Grenton network.
- **New device missing:** check that its widget is in the mobile interface, then run **Reconfigure**. A legacy `ROLLER_SHUTTER` widget has different support from `ROLLER_SHUTTER_V3`.
- **Old dialogs or missing settings after an update:** confirm the installed integration version, fully restart Home Assistant, then refresh the browser.
- **States update slowly:** inspect the CLU's Connection diagnostics and polling interval. Temperature and HTTP Gate values use polling. Repeated failed reads can make entities unavailable.
- **Settings fail to save:** complete the dialog and check **Settings → System → Logs**. Device writes are checked by reading back the result; a failed multi-setting save can leave partial changes, so reopen configuration to inspect current values.
- **Telnet capture fails:** close other log clients, then enable the capture switch again. Disconnected streams do not reconnect automatically.

## Security and privacy

Keep Object Manager and CLU services on a trusted local network; do not expose their management, Telnet or TFTP ports to the internet. Imported interfaces contain encryption keys, and diagnostic files or logs may contain installation details. Remove secrets before sharing captures or backups. See [security guidance and private vulnerability reporting](SECURITY.md).

## Advanced use and development

- [Device and protocol reference](docs/reference.md): detailed behavior, attribute bindings, schedule encoding and automation examples for `grenton.run_script`, `grenton.run_scene`, and `grenton.set_thermostat_schedule`.
- [CLI guide](examples/cli/README.md): standalone diagnostics, attribute/variable inspection and file downloads. The CLI is a separate developer tool and is not installed by HACS.
- [Contributing](CONTRIBUTING.md): development setup and test commands.
- [Release notes](CHANGELOG.md).

## Support and license

[Report an issue](https://github.com/k911/homeassistant-grenton/issues) or [ask a question](https://github.com/k911/homeassistant-grenton/discussions). Include the Home Assistant and integration versions, reproduction steps, and sanitized logs.

Licensed under [GPL-3.0](LICENSE). Thanks to [@sszczep](https://github.com/sszczep), author of the original integration, and the Home Assistant community.
