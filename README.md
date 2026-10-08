# Home Assistant Grenton

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/sszczep/homeassistant-grenton?style=for-the-badge)](https://github.com/sszczep/homeassistant-grenton/releases)
[![Maintenance](https://img.shields.io/badge/Maintained%3F-yes-green.svg?style=for-the-badge)](https://github.com/sszczep/homeassistant-grenton/graphs/commit-activity)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-%E2%9D%A4-red?style=for-the-badge&logo=github)](https://github.com/sponsors/sszczep)

Custom Home Assistant integration for **Grenton Smart Home** system. Seamlessly connect and control your Grenton devices through Home Assistant with automatic discovery, real-time updates, and full configuration support.

![Grenton Integration](assets/dashboard.png)

## 💖 Sponsor

If you find this integration helpful, please consider supporting ongoing development via [**GitHub Sponsors**](https://github.com/sponsors/sszczep).

Your support helps maintain features, fix bugs, and improve documentation.

## ✨ Features

- **🔌 Automatic Device Discovery** - Connects to Grenton Object Manager and automatically discovers all configured devices
- **CLU Controllers and Scripts** - Discovers every CLU from the imported interface and runs its scripts with dynamic typed arguments
- **🏠 Multiple Widget Types** - Support for lights, switches, dimmers, sensors, covers, and more
- **⚙️ Per-Entity Configuration** - Customize device class and unit of measurement for each supported entity through the UI
- **🔄 Real-time Updates** - Automatic state synchronization with Grenton system
- **🌍 Multi-language Support** - Fully translated UI in English and Polish
- **📱 Modern UI** - Native Home Assistant integration with clean configuration flows

## 📦 Supported Widgets

| Widget Type (Literal) | Description |
|-----------------------|-------------|
| **VALUE_V2** | Single value exposed as a configurable `sensor` or `binary_sensor`. |
| **VALUE_DOUBLE** | Two values (A/B), each independently configurable as `sensor` or `binary_sensor`. |
| **ON_OFF** | Single relay exposed as a configurable `switch` or on/off `light` (default: switch). |
| **ON_OFF_DOUBLE** | Dual relays with each channel configurable as `switch` or on/off `light`. |
| **DIMMER_V2** | Dimmable light exposed as `light` with brightness (0-100%). |
| **LED** | LED or RGB/RGBW light exposed as `light` with brightness and color where available. |
| **CONTACT_SENSOR** | Contact/door/window sensor exposed as `binary_sensor`. |
| **CONTACT_SENSOR_DOUBLE** | Dual contact channels exposed as two `binary_sensor` entities. |
| **SLIDER** | User-configurable numeric control exposed as `number` (slider/box/auto modes). |
| **MULTISENSOR** | Composite multi-channel sensor mapped to multiple `sensor` entities (as provided). |
| **ROLLER_SHUTTER** | Legacy roller shutter exposed as `sensor` (state enum) plus a `button` action entity. |
| **ROLLER_SHUTTER_V3** | Roller shutter V3 exposed as a single `cover` entity (position; lamel/tilt when available). |
| **CAMERA** | Camera stream exposed as `camera`. |
| **SCENE** | Scene buttons with configurable script, method, attribute, or variable actions and arguments. |

## 🚀 Installation

### Option 1: HACS (Recommended)

1. Open [HACS](https://www.hacs.xyz/docs/use/download/download/) in your Home Assistant instance
2. Go to **Integrations**
3. Click the three dots in the top right corner
4. Select **Custom repositories**
5. Add this repository URL: `https://github.com/sszczep/homeassistant-grenton`
6. Select **Integration** as the category
7. Click **Install**
8. Restart Home Assistant

### Option 2: Manual Installation

1. Download the latest release from the [Releases](https://github.com/sszczep/homeassistant-grenton/releases) section
2. Extract the zip file
3. Copy the `homeassistant_grenton` folder into your `custom_components` directory (usually `/config/custom_components/`)
4. Restart Home Assistant

## 📖 Configuration

### Initial Setup

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for **"Grenton"**
4. Enter your Grenton Object Manager connection details:
   - **IP Address**: IP address of your Grenton Object Manager
   - **Port**: Port number (default: `9998`)
   - **PIN**: Your Object Manager PIN code

### Entity Configuration

After initial setup, you can customize individual entities:

1. Navigate to **Settings** → **Devices & Services** → **Grenton**
2. Click **Configure** on the integration
3. Select an entity from the dropdown menu
4. Configure entity properties:
   - **Sensors**: Choose device class and unit of measurement
   - **Sliders (Numbers)**: Set display mode, device class, and unit
   - **Binary Sensors**: Select appropriate device class
   - **On/Off controls**: Choose whether the entity is a switch or a light
   - **Scenes**: Review or change the action call type, CLU, script/object name, index, and arguments/value

The integration intelligently filters available options based on your selections and automatically skips unnecessary configuration steps.

You can also open a Grenton device under **Settings → Devices & Services → Devices**
and click **Visit device**. This opens a configuration popup listing the entities
in that widget/device, with entity selection limited to that device. Select an
editable entity to review and change its settings through the same forms above.
Entities without additional settings, including CLU controller sensors, are
listed as read only. Closing the popup before saving leaves the settings unchanged.

Widget devices are automatically named from their entity labels. Two distinct
labels are joined with `·`; devices with more labels show the first two and
`(+N)` for the remaining ones. The widget type remains in the Model field.
The popup lists every entity and includes a **Device name** field with a separate
**Save name** button. Leave it empty and save to restore the automatic name.
Names use Home Assistant's native device rename setting, so a name set in either
UI is respected across reloads and interface refreshes. Renaming preserves the
device's room, entity IDs and entity labels, and does not reload the integration.
For a widget with one entity, its default friendly name follows the device name
without repeating the label. Widgets with multiple entities keep their individual
entity labels, and existing custom entity names remain unchanged.

### VALUE_V2 and VALUE_DOUBLE as Binary Sensors

Select the value entity in the configuration popup and choose **Binary sensor**,
then select a device class such as **Door**, **Window**, or **Opening**. Zero and
negative values become off; positive values become on. Numeric strings are also
accepted, while missing or invalid values become unknown. Binary sensors have no
measurement unit or state class.

For contacts that report `1` when closed and `0` when open, select the **Door**,
**Window**, or **Opening** class and enable **Invert state** in the binary sensor
form. This makes positive values closed/off and zero open/on. Inversion is
configured separately for each VALUE_DOUBLE channel; missing or invalid values
remain unknown. It defaults to disabled. The setting describes the reported
value directly, rather than assuming its meaning from NO/NC wiring.

The default remains **Sensor**, with configurable device class and unit. Enum
sensors have no measurement state class or unit. Changing between Sensor and
Binary sensor reloads the integration and replaces the old entity registration
on the same device. Update automations and dashboards to use the new entity ID.
For VALUE_DOUBLE, configure each of the two entities independently; they can use
different domains, device classes, and units while remaining on the same device.

### On/Off Controls as Lights

Open **Grenton → Configure** and select the ON_OFF entity. The dialog has three
forms, prefilled with the current imported or saved settings:

1. **State**: choose Switch or Light and configure the state source (CLU and
   object/attribute index, or CLU and variable name).
2. **Turn on**: configure the action's call type, CLU and target.
3. **Turn off**: configure its action independently and save all settings.

Both actions support Method, Script, Attribute and Variable. Method/script calls
have typed argument rows that can be added, removed or reordered; omit trailing
arguments to use defaults. Attribute/variable actions have an editable value to
set. State sources support Attribute and Variable. The entity's attributes expose
the effective state source and both actions, including arguments and call payloads.

Existing controls default to Switch. ON_OFF_DOUBLE channels can
be configured independently.

Saving reloads the integration and creates the selected entity type, preserving
the Grenton state subscription, ON/OFF actions, and device. Light mode provides
on/off control without brightness or color controls. The choice persists across
restarts and interface reconfiguration.

Changing the type changes the entity domain (`switch.…` ↔ `light.…`) and removes
the previous entity registration. Update dashboards and automations to reference
the new entity ID. You can change the choice again through the same dialog.

### CLU Controllers and Scripts

Each CLU in the imported configuration is automatically added as a Home
Assistant device, including CLUs without widgets. Its **Controller**
sensor identifies the CLU by serial number and exposes `clu_id`, `ip`, and
`port`. Each CLU has its own entity, so installations with multiple CLUs can
select the controller on which to run a script. Refresh the interface through
**Reconfigure** when CLUs are added or removed.

The CLU type is detected from its serial number: `221…` is `CLU_Z_WAVE`,
and `521…` is `CLU_GATE_HTTP`. The detected type is shown as the device model.
Built-in values use indexed attributes on the CLU object ID imported from the
interface, for example `CLU828599.0` for Uptime. The object ID can differ from
`CLU` followed by the serial number; serial numbers are used only to detect
the device type.

| Entity | Index | Domain | Availability / purpose |
| --- | --- | --- | --- |
| Uptime | 0 | `sensor` | Both types; read-only running time in seconds (`duration`). |
| Cloud connection | 19 | `binary_sensor` | Both types; read-only Connected / Disconnected status (`connectivity`). |
| Use cloud | 18 | `switch` | Both types; enable or disable the CLU cloud connector. |
| Firmware version | 17 | `sensor` | Both types; read-only software version, also shown in the device's firmware metadata. |
| Bus voltage | 27 | `sensor` | CLU_Z_WAVE only; read-only voltage in V, with measurement statistics. |

Read-only values appear under **Diagnostic**; Use cloud appears under
**Configuration**. They share the existing CLU device and receive values
through attribute subscriptions. Use cloud writes a boolean to attribute 18
and reads the CLU state back. Values not yet reported remain unknown.
Unrecognized serial prefixes still get a Controller entity for script calls;
only supported types get built-in attribute entities.

The constants in `domain/device_types.py` define `index: attribute name` maps
for each known device type and the CLU serial prefixes. Extend these maps when
adding types; the entity bindings use the mapped indexes and skip unsupported
attributes.

Open the CLU device's **Visit device** link to configure custom variables.
Choose **Add variable**, enter its Grenton variable name and optional display
name, and select its Grenton type:

- **Boolean** creates a switch that writes `true` / `false` and reads the CLU
  state back after a change.
- **String**, **Integer**, and **Float** create sensors with the same device
  class and unit settings as VALUE_V2. Select **None** for a plain value sensor.

The same dialog lets you edit or remove a configured variable. These are
existing Grenton variables selected for exposure in Home Assistant; adding or
removing an entity does not create or delete a variable on the CLU. Their
configuration is retained when the interface is refreshed. Renaming a variable
keeps its Home Assistant identity; changing between Boolean and a sensor type
changes its entity domain, so update references to its entity ID as needed.
Custom variable entities also appear in the integration's **Configure entity**
list. Configuration changes reload the integration.

Use **Developer Tools → Actions → Grenton: Run script** (`grenton.run_script`)
to select a CLU controller entity or device, enter its script name, and add
optional typed argument rows. This calls the script directly on the CLU;
no scene widget or saved button is required. Enter the script name as defined
on that CLU.

For example, replace the entity ID below with your CLU's Controller sensor:

```yaml
action: grenton.run_script
target:
  entity_id: sensor.main_clu_controller
data:
  script: Evening
  arguments:
    - type: string
      value: "living_room"
    - type: float
      value: 0.75
    - type: boolean
      value: true
```

This sends `Evening("living_room", 0.75, true)` to the selected CLU. Arguments
are positional and support `string`, `number`, `float`, `boolean`, `nil`, and
`lua`. Strings are quoted and escaped automatically. Omit trailing arguments
to use defaults implemented by the Grenton script. Omit `arguments`, or pass
`arguments: []`, to call with no arguments.

Automation and Home Assistant script templates can supply both the script name
and argument values at run time:

```yaml
action: grenton.run_script
target:
  entity_id: sensor.main_clu_controller
data:
  script: SetOutput
  arguments:
    - type: number
      value: "{{ states('input_number.output_value') | int }}"
```

Scene buttons run their saved action when pressed.

The upstream **Grenton: Run scene** action (`grenton.run_scene`) remains available
for existing automations targeting a SCENE button. Its optional `parameter` is
raw Lua passed into the script call for that invocation only:

```yaml
action: grenton.run_scene
target:
  entity_id: button.evening
data:
  parameter: '"lightOffice", -1'
```

Omit `parameter` to use the scene's saved arguments/value. Set `parameter: ""`
to call the script without arguments. Runtime overrides do not change the scene
configuration. Use `grenton.run_script` to call a named script directly on a CLU
with typed arguments.

### Scene Actions and Arguments

Select a scene button in **Grenton → Configure** to view its current settings.
Changing the call type updates the fields immediately in the same popup:

- **Script**: CLU, script name, and optional arguments.
- **Method**: CLU, object name, method index, and optional arguments.
- **Attribute**: CLU, object name, and attribute index.
- **Variable**: CLU and variable name (text).

Saving applies changes immediately and preserves them across reloads and
interface refreshes. These settings override the scene in Home Assistant only.

For script and method calls, use **Add argument** to add individual input rows.
Each row has a type picker: **String**, **Number**, **Float**, **Boolean**, **Nil**, or
**Lua expression**. String arguments are quoted and escaped automatically;
numbers and booleans keep their types. Edit a row directly, remove it, or use
the up/down controls to change the order. Omit trailing arguments to let the
script/method use its defaults; remove every row to call without arguments.
Imported script and method arguments appear as separate rows when they can be
safely split, with types inferred from Lua literals. For example, `800,0` becomes
two Number arguments, while `"800,0"` remains one String argument. Complex Lua
is preserved as an expression. Attribute/variable calls retain their existing
value while their target is configured.

Open the scene button's entity details and expand **Attributes** to see
`call_type`, `clu_id`, `object_name`, `event`, `index` (for methods/attributes),
`variable_name` (for variables), `value`,
`arguments` (including each argument's type and value for scripts/methods),
and `call`, the exact configured call payload. These are also available in
**Developer Tools → States**.

After updating the integration, restart Home Assistant and refresh the browser
to load the scene editor.

## 🎨 Device Classes & Units

### Sensor Device Classes

The integration supports all Home Assistant sensor device classes:

- **Environmental**: Temperature, Humidity, Atmospheric Pressure, Air Quality Index
- **Energy**: Power, Energy, Voltage, Current, Apparent Power, Reactive Power
- **Environmental Monitoring**: CO, CO2, VOC, PM1, PM10, PM2.5
- **Physical**: Distance, Speed, Weight, Volume, Pressure
- **And many more...**

### Number (Slider) Device Classes

Configure sliders with appropriate device classes for proper representation:

- Display modes: Auto, Slider, Box
- Device classes: Temperature, Humidity, Power, Voltage, Current, etc.
- Automatic unit filtering based on device class

### Binary Sensor Device Classes

- **Safety**: Battery, Cold, Heat, Gas, Smoke, CO
- **Security**: Door, Window, Opening, Lock, Motion, Occupancy
- **Connectivity**: Connectivity, Power, Plug
- **System**: Problem, Running, Update, Vibration

## 🐛 Troubleshooting

### Connection Issues

**Cannot connect to Grenton Object Manager:**

1. Verify IP address and port are correct
2. Ensure Home Assistant can reach the Grenton network
3. Check PIN code is correct
4. Review Home Assistant logs: **Settings** → **System** → **Logs**

### Configuration Not Saving

**Entity configurations don't persist:**

1. Complete all configuration steps
2. Ensure you clicked **Submit** on the final step
3. Check integration logs for errors
4. Restart Home Assistant and try again

### Device Not Appearing

**Device discovered but not showing in UI:**

1. Check device is properly configured in Grenton Object Manager
2. Verify device type is supported (see Supported Devices table)
3. Review coordinator logs for device mapping errors
4. Force integration reload: **Settings** → **Devices & Services** → **Grenton** → **⋮** → **Reload**

### State Not Updating

**Device state not synchronized:**

1. Check Grenton Object Manager is running and accessible
2. Verify network connectivity between Home Assistant and Grenton
3. Review coordinator update interval (default: 30 seconds)
4. Check Grenton device is responding to queries

## 📝 License

This project is licensed under the **GNU General Public License v3.0**.

- You may run, study, share, and modify the software under the terms of GPL-3.0.
- Redistributions and derivative works must also be licensed under GPL-3.0 and include the source code.

See the full text in the [LICENSE](LICENSE) file.

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Commit your changes: `git commit -m 'Add amazing feature'`
4. Push to the branch: `git push origin feature/amazing-feature`
5. Open a Pull Request

## 📧 Support

For issues, questions, or contributions:

- **GitHub Issues**: [Report a bug or request a feature](https://github.com/sszczep/homeassistant-grenton/issues)
- **Discussions**: [Ask questions and share ideas](https://github.com/sszczep/homeassistant-grenton/discussions)
- **GitHub Sponsors**: [Sponsor ongoing development](https://github.com/sponsors/sszczep)

## 💖 Sponsor

If you find this integration helpful, please consider supporting ongoing development via [**GitHub Sponsors**](https://github.com/sponsors/sszczep).

Your support helps maintain features, fix bugs, and improve documentation.

## 🙏 Acknowledgments

- Home Assistant community for the excellent platform
- Grenton for their smart home system
- All contributors and users of this integration

---

<div align="center">

**Made with ❤️ for the Home Assistant community**

[Report Bug](https://github.com/sszczep/homeassistant-grenton/issues) · [Request Feature](https://github.com/sszczep/homeassistant-grenton/issues) · [Discuss](https://github.com/sszczep/homeassistant-grenton/discussions)

</div>
