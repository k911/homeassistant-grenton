# Grenton CLI Configuration Tool

A command-line tool for configuring and testing Grenton Object Manager connections.

## Installation

1. use venv

    ```bash
    export VENV_NAME="venv"
    python33 -m venv "$VENV_NAME"
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
    python3 main.py configure --pin 1234 --config /path/to/custom/config.json

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
~/.grenton/config.json
```

You can specify a custom path with the `--config` option. The parent directory will be created automatically if it doesn't exist.

## Options

- `--url`: Grenton Object Manager URL (default: `http://localhost:9998`)
- `--pin`: Grenton Object Manager PIN (required)
- `--config`: Configuration file path (default: `~/.grenton/config.json`)

## What it does

1. Connects to the Grenton Object Manager API
2. Fetches the mobile interface configuration
3. Parses the ZIP file and extracts interface data
4. **Saves configuration to `~/.grenton/config.json`**
5. Displays all configured CLUs with their details
6. Tests the connection to each CLU
7. Outputs connection status for each CLU

## Exit codes

- `0`: All CLUs connected successfully
- `1`: Connection or authentication error
