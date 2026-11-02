"""Read selected values once without creating CLU subscriptions."""

import argparse
import asyncio
import json
import logging
import re

from config import get_config_path
from grenton_api import GrentonManager

from .watch_attribute import _parse_attribute

_LOGGER = logging.getLogger(__name__)


def _parse_read_attribute(spec: str) -> tuple[str, str]:
    object_name, index = _parse_attribute(spec)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", object_name) or not re.fullmatch(
        r"[0-9]+", index
    ):
        _LOGGER.error(
            "✗ Invalid --attribute '%s': use a Lua object name and a non-negative numeric index",
            spec,
        )
        raise SystemExit(2)
    return object_name, index


def _format_output(result: dict, output_format: str, *, summary: bool) -> str:
    if output_format == "json":
        return json.dumps(result, indent=2, ensure_ascii=False)
    lines = []
    for record in result["values"]:
        if record.get("status") == "missing":
            rendered = "<read failed>"
        elif summary:
            rendered = record["type"]
            if "length" in record:
                rendered += f" ({record['length']} characters)"
        else:
            rendered = repr(record["value"])
        lines.append(f"{record['key']}: {rendered}")
    return "\n".join(lines)


def fetch_values_command(args: argparse.Namespace) -> None:
    attributes = [_parse_read_attribute(spec) for spec in args.attribute]
    result = asyncio.run(
        GrentonManager().fetch_values(
            get_config_path(args.config),
            args.clu,
            attributes,
            args.variable,
            summary=args.summary,
        )
    )
    print(_format_output(result, args.output, summary=args.summary))
    if not result["complete"]:
        raise SystemExit(1)
