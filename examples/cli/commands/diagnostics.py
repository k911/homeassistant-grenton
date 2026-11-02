"""Display CLU diagnostic exports and installed device firmware inventory."""

import argparse
import asyncio
import json
import math

from config import get_config_path
from grenton_api import GrentonManager


def report_timeout(value: str) -> float:
    """Reject invalid/unbounded deadlines before opening sockets."""
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number in (0, 300]") from error
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise argparse.ArgumentTypeError("timeout must be between 0 (exclusive) and 300 seconds")
    return timeout


def format_report(report: dict, kind: str, output: str) -> str:
    """Keep the full JSON export or render a compact device table."""
    if output == "json":
        return json.dumps(report, indent=2, ensure_ascii=False)
    fields = [
        ("Serial", "sn"), ("Status", "status"), ("Firmware", "fwVer"),
        ("HW type", "hwType"), ("HW rev", "hwVer"),
        ("FW type", "fwType"), ("FW API", "fwApiVer"),
    ]
    if kind == "diagnostics":
        fields.extend([("Voltage", "voltage"), ("Fails", "fails"), ("Banned", "banned")])
        arrays = (("TF-bus", "tfbusDiagnostic"), ("Z-Wave", "zwaveDiagnostic"))
    else:
        arrays = (("TF-bus", "tfbusDevices"), ("Z-Wave", "zwaveDevices"))
    rows = [["CLU", *[str(report.get(key, "-")) for _, key in fields]]]
    for bus, key in arrays:
        rows.extend([bus, *[str(device.get(field, "-")) for _, field in fields]]
                    for device in report[key])
    headers = ["Bus", *[title for title, _ in fields]]
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]
    lines = ["  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
             for row in [headers, *rows]]
    lines.extend(f"{bus} devices: {len(report[key])}" for bus, key in arrays)
    if kind == "diagnostics":
        lines.extend(f"{label}: {report[key]}" for label, key in (
            ("CLU address", "ipAddress"), ("Cloud connected", "cloudConn"),
        ) if key in report)
        lines.append("Fails are reported device counters; their time window is unspecified.")
    return "\n".join(lines)


def diagnostics_command(args: argparse.Namespace) -> None:
    """Fetch a fresh diagnostic pack or installed firmware inventory."""
    manager = GrentonManager()
    method = manager.diagnostics if args.report_kind == "diagnostics" else manager.firmware
    report = asyncio.run(method(get_config_path(args.config), args.clu, timeout=args.timeout))
    print(format_report(report, args.report_kind, args.output))
