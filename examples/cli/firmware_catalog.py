"""Compare installed versions with Grenton's stable Object Manager index."""

import re

import aiohttp

FIRMWARE_INDEX_URL = "https://objectmanager.grenton.com/firmware/v5/firmware.list"
MAX_INDEX_BYTES = 65536
_VERSION = r"(\d+)\.(\d+)\.(\d+)-(\d{4,6})([A-Z]?)"
_FILE = re.compile(
    rf"([A-Z0-9_]+)-(\d+)-(\d+)-(\d+)-({_VERSION})\.fw"
)


def version_key(value: str) -> tuple[int, int, int, int, str]:
    """Compare numeric components, preserving a build's letter revision."""
    match = re.fullmatch(_VERSION, value)
    if match is None:
        raise ValueError("Unrecognized installed firmware version")
    return (*map(int, match.groups()[:4]), match[5])


def parse_catalog(text: str) -> dict[tuple[int, int, int], tuple[str, str]]:
    """Ignore JSON duplicates, shell prompts and unrelated index entries."""
    catalog = {}
    for line in text.splitlines():
        match = _FILE.fullmatch(line.strip())
        if match is None:
            continue
        signature = tuple(map(int, match.group(2, 3, 4)))
        version = match[5]
        previous = catalog.get(signature)
        if previous is None or version_key(version) > version_key(previous[0]):
            catalog[signature] = (version, match[0])
    if not catalog:
        raise ValueError("Firmware index contains no recognized firmware files")
    return catalog


async def fetch_catalog(timeout: float) -> dict:
    """Fetch metadata only, with a total deadline and bounded decoded bytes."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(FIRMWARE_INDEX_URL, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"Firmware index returned HTTP {response.status}")
            data = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                data.extend(chunk)
                if len(data) > MAX_INDEX_BYTES:
                    raise ValueError("Firmware index exceeds the 64 KiB metadata limit")
    return parse_catalog(data.decode("utf-8"))


def compare_device(device: dict, catalog: dict) -> dict:
    """Match all hardware/firmware type fields; never infer from a device name."""
    result = {"installed_version": device.get("fwVer")}
    try:
        values = [device.get(key) for key in ("hwType", "hwVer", "fwType")]
        if any(isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)) for value in values):
            raise ValueError("Missing or invalid hardware/firmware type fields")
        signature = tuple(map(int, values))
        installed = version_key(device.get("fwVer", ""))
    except (TypeError, ValueError) as error:
        return {**result, "status": "unknown", "reason": str(error)}
    latest = catalog.get(signature)
    if latest is None:
        return {**result, "status": "unknown", "reason": "No matching hardware/firmware type in stable index"}
    version, filename = latest
    available = version_key(version)
    status = "update_available" if installed < available else (
        "up_to_date" if installed == available else "newer_than_stable"
    )
    return {**result, "status": status, "latest_version": version, "filename": filename}


async def check_firmware(report: dict, kind: str, *, timeout: float) -> dict:
    """Repository failure must not hide successfully downloaded diagnostics."""
    result = {"source": FIRMWARE_INDEX_URL, "channel": "stable"}
    try:
        catalog = await fetch_catalog(timeout)
    except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError) as error:
        return {**result, "status": "unavailable", "reason": str(error) or type(error).__name__}
    result.update(compare_device(report, catalog))
    arrays = ("tfbusDiagnostic", "zwaveDiagnostic") if kind == "diagnostics" else (
        "tfbusDevices", "zwaveDevices"
    )
    result["devices"] = [
        {"sn": device.get("sn"), "bus": bus, **compare_device(device, catalog)}
        for bus in arrays for device in report.get(bus, [])
    ]
    return result
