# Grenton CLI Configuration Tool

A command-line tool for configuring and testing Grenton Object Manager connections.

## Installation

1. use venv

    ```bash
    export VENV_NAME="venv"
    python3 -m venv "$VENV_NAME"
    source "$VENV_NAME/bin/activate"
    ```

2. Install dependencies:

    ```bash
    brew install jpeg-turbo
    pip install -r requirements.txt
    ```

3. Make the script executable:

    ```bash
    chmod +x main.py
    ```

## Usage

### Basic usage (localhost)

    ```bash
    python3 main.py configure --pin 1234
    ```

### Connect to remote Object Manager

    ```bash
    python3 main.py configure --url http://192.168.1.100:9998 --pin 5678
    ```

### Using custom configuration path

    ```bash
    python3 main.py configure --pin 1234 --config /path/to/custom/config.yaml

    ```

### Update tracked objects from the interface

By default, `configure` leaves `grenton_tracked_objects` unchanged. To discover
all variables and attributes referenced anywhere in the fetched interface and
merge missing ones into the configuration, use:

```bash
python3 main.py configure --pin 1234 --update-tracked-objects
```

Existing group names and item customisations (for example `description` or
`sync`) are retained. Variables are matched by CLU name and variable name;
attributes are matched by object ID and index. Repeated references in the
interface are added only once, using the first referencing widget/component
label for a newly created group. Attribute indices are written as numbers. If
one label contains multiple attributes, their interface object-field names are
added as descriptions so the entries remain distinguishable.

### Set an attribute or call a method

The `execute` command calls a method by default. Use `--call-type attribute` to
write an attribute through the CLU API, without sending a new Object Manager
configuration. For example, this sets thermostat attribute 10 (minimum
temperature) to 10°C when you choose to run it:

```bash
python3 main.py execute --clu CLU_Z_WAVE_1 --object THE6183 \
  --call-type attribute --action 10 --value 10
```

Use the CLU name from your saved configuration. Attribute 11 controls the maximum
temperature. The existing method syntax remains available:

```bash
python3 main.py execute --clu CLU_Z_WAVE_1 --object DOU0699 --action 2 --value 1
```

Values are passed as quoted strings, matching the mobile interface's attribute
and method actions. Successful writes print `set(...)`; rejected calls and
connection failures exit with code 1.

### Stream CLU debug logs

Set the CLU's log level and follow its Telnet output until Ctrl+C:

```bash
python3 main.py debug-logs --clu CLU_Z_WAVE_1
python3 main.py debug-logs --clu CLU_Z_WAVE_1 --level INFO
```

`logs` is an alias for `debug-logs`. `--level` accepts `OFF`, `ERROR`, `WARNING`,
`INFO`, and `DEBUG` (case insensitive), mapped to CLU attribute 31 values 0–4.
The default is `DEBUG`. The command selects the CLU by its name in the saved
configuration and writes its imported object ID's attribute through the encrypted
UDP API. Use `--config /path/to/config.yaml` for another configuration file.

Logs go directly to stdout, so they can be saved with shell redirection;
connection status and retry messages go to stderr:

```bash
python3 main.py debug-logs --clu CLU_Z_WAVE_1 > clu.log
```

The listener uses Telnet port 23 by default, negotiates options like Object
Manager, and sends CRLF keepalives every five seconds. `--port` selects another
existing Telnet port; it does not change the CLU's port configuration. Connection
attempts and initial negotiation have a five-second timeout each. A CLU supports
only one Telnet connection: stop logging in Object Manager before using this
command. If the initial session cannot be established, the command warns and
exits with code 1 rather than retrying indefinitely. Once a session has been
established, EOF or a connection failure triggers a retry after one second and
reapplies the selected level, including after a source
deployment resets the setting during `OnInit`. No Telnet username or password
exchange is used, matching the captured CLU behavior.

Ctrl+C closes both local UDP and Telnet connections. The selected CLU log level
remains set. To disable logging explicitly and exit without opening Telnet:

```bash
python3 main.py debug-logs --clu CLU_Z_WAVE_1 --level OFF
```

The global `--debug` option controls the CLI's own diagnostic messages on stderr;
`debug-logs --level` controls the CLU's verbosity. Custom Lua logging queues may
have their own destinations; this command streams the CLU's Telnet output.

### Read CLU diagnostics and installed firmware versions

Fetch a fresh diagnostic snapshot with CLU health, cloud connection, bus voltages,
device status, failure counters, and installed firmware:

```bash
python3 main.py diagnostics --clu CLU_Z_WAVE_1
python3 main.py diagnostics --clu CLU_Z_WAVE_1 --output json > diagnostics.json
```

Refresh and list the currently installed firmware versions for the CLU and its
TF-bus and Z-Wave devices:

```bash
python3 main.py firmware --clu CLU_Z_WAVE_1
python3 main.py firmware --clu CLU_Z_WAVE_1 --output json > firmware.json
```

The text output lists serial numbers, status, firmware versions, hardware type
and revision, firmware type and API version. Diagnostics also includes reported
voltages, failure counters and banned flags where available. A dash means the
export does not provide that field. Failure-counter time windows are unspecified;
the numbers do not by themselves identify recent failures. JSON output preserves
the full exported fields, including any additional metadata.

Both commands select the CLU by its name in the saved configuration. They accept
`--config /path/to/config.yaml`, `--output text|json` (also `--format`), and
`--timeout 10` for the management request deadline in seconds (maximum 300).
The diagnostics command requests `req_diagnostic_refresh`; the firmware command
requests `req_refresh_config`. Each then activates the transfer service with
`req_start_ftp` and downloads the appropriate JSON export over the captured
fixed-port TFTP protocol on UDP/69. TFTP preserves raw bytes and retries lost
blocks, with a 60-second transfer deadline and a 4 MiB size limit. Management
messages use the configured encrypted UDP connection, normally port 1234.

These commands read installed versions; they do not query a vendor release
catalog or upload firmware/source code. They fail with exit code 1 on request,
transfer or export-validation errors. All sockets close on completion, error or
cancellation.

### Watch attribute changes

Attributes are specified as `OBJECT.INDEX`:

```bash
python3 main.py watch-attribute --attribute DIN5696.0
```

Repeat `--attribute` to watch more than one attribute:

```bash
python3 main.py watch-attribute --clu CLU_Z_WAVE_1 \
  --attribute DIN5696.0 \
  --attribute DIN5697.1
```

### Fetch selected values once

`fetch-values` reads attributes and variables through `SYSTEM:fetchValues` on
one control socket. It creates no realtime subscriptions and sends no writes.

```bash
python3 main.py fetch-values --clu CLU_Z_WAVE_1 \
  --attribute CLU828599.0 --attribute THERMOSTAT1.14 --variable Counter
```

Repeat `--attribute OBJECT.INDEX` or `--variable NAME` as needed. For a safe
diagnostic capture, `--summary` prints only types and string lengths, and also
suppresses raw packet DEBUG logging even when `--debug` is enabled:

```bash
python3 main.py fetch-values --clu CLU_Z_WAVE_1 \
  --attribute THERMOSTAT1.9 --summary --format json
```

Use object IDs from your configuration. Output defaults to text; `--format json`
(`--output json` is an alias) produces a single result document. A real `nil`
is distinct from a failed read. Failed or incomplete reads exit with code 1;
an empty selection exits with code 2. The socket closes after the read, including
on failure or cancellation.

### Test subscription cleanup

```bash
python3 main.py test-client-destroy --clu CLU_Z_WAVE_1
```

Creates one temporary subscription to CLU uptime with its own ID and socket,
then sends `SYSTEM:clientDestroy(0,0,id)` on that socket. It observes reports
for 10 seconds before and after destruction, checks that the CLU still answers,
and closes the socket. No object values are changed. Output contains counts and
verification flags, without raw values. `--observe-seconds 5–15` changes each
observation window. Only `passed` exits with code 0; `failed` and `inconclusive`
exit with code 1. Acknowledgement alone does not establish successful cleanup.

### Test the shared report listener

```bash
python3 main.py test-subscriptions --clu CLU_Z_WAVE_1
```

Creates two small, temporary subscriptions to changing CLU counters. The test
checks ID routing through one listener, 10-second health pings, two 45-second
renewals, changing the initial ID, and explicit cleanup. It then keeps the
listener open for 10 seconds to verify that incoming reports actually stop.
The default run takes about two minutes and changes no object values. Output
contains counts and verification flags. `--duration 100` controls the first
observation window (100–300 seconds).

### Help:

```bash
python3 main.py --help
python3 main.py configure --help
```

## Configuration Storage

After a successful configuration, the tool automatically saves:

- Encryption details
- CLU objects (devices)
- Interface metadata

By default, configuration is stored in:

```
~/.grenton/config.yaml
```

The interface cache (JSON) is stored by default in:

```
~/.grenton/cache/interface.json
```

You can specify a custom config path with the `--config` option. The parent directory will be created automatically if it doesn't exist. Inside `config.yaml` you can optionally set the cache path using either a top-level `cache_path: /path/to/interface.json` or a `cache:
 interface: /path/to/interface.json` mapping.

### Tracked objects (optional)

You can add extra variables/attributes to track even if they are not present in the interface cache. These will be merged with objects discovered from the cache. The mapping is per CLU name.

```yaml
grenton_tracked_objects:
  CLU_Z_WAVE_1:
    variables:
      "Garage door status":
        - name: GarageDoorStatusText
          description: Optional description
    attributes:
      "Front door power":
        - object: DOU0699
          index: 0
          description: Optional description
```

## Options

- `--url`: Grenton Object Manager URL (default: `http://localhost:9998`)
- `--pin`: Grenton Object Manager PIN (required)
- `--config`: Configuration file path (default: `~/.grenton/config.yaml`)
- `--update-tracked-objects`: Merge interface variables and attributes into tracked objects
- `--debug`: Enable debug logging output

## What it does

1. Connects to the Grenton Object Manager API
2. Fetches the mobile interface configuration
3. Parses the ZIP file and extracts interface data
4. **Saves configuration to `~/.grenton/config.yaml`**
5. Displays all configured CLUs with their details
6. Tests the connection to each CLU
7. Outputs connection status for each CLU

## Exit codes

- `0`: All CLUs connected successfully
- `1`: Connection or authentication error

## Tests

From this directory, run `python3 -m unittest test_config test_execute test_watch test_fetch_values test_client_destroy`.
The action tests mock the CLU connection and send no commands to live hardware.

## Watch implementation

Watch commands use the same `GrentonCluApi` as Home Assistant: one shared report
listener, unique IDs for groups of up to 30 keys, and one control-socket health
ping every 10 seconds. Groups renew every 45 seconds with fresh request IDs.
Report parsing is shared too,
including comma-containing and ambiguously quoted strings. Failed reads do not
overwrite the last displayed values. One-off state reads use `SYSTEM:fetchValues`
on the control socket, with individually marked large attributes using direct
`OBJECT:get(INDEX)` instead. These reads create no realtime subscriptions.
Keepalive and registration timers run independently; stopping a watch cancels
both tasks before closing its sockets.
