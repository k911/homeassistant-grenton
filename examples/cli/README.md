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

## Watch implementation

The watch commands use `custom_components/homeassistant_grenton/domain/api/clu_cli.py`.
This CLI-specific implementation keeps subscriptions on one UDP socket and
parses comma-containing quoted values using the number of registered keys.
It is intentionally separate from the upstream `clu.py` and
`clu_messages/client_register.py` files so upstream rebases do not require
reapplying CLI-specific changes to those files.
