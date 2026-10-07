"""Watch attribute command to follow CLU object attribute value changes."""
import argparse
import asyncio
import logging
import sys

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)

_SEPARATORS = (".", ":", ",")


def _parse_attribute(spec: str) -> tuple[str, str]:
    """Parse an OBJECT.INDEX attribute specification."""
    for separator in _SEPARATORS:
        if separator in spec:
            object_name, _, index = spec.partition(separator)
            object_name, index = object_name.strip(), index.strip()
            if object_name and index:
                return object_name, index
            break

    _LOGGER.error("✗ Invalid --attribute '%s', expected format OBJECT.INDEX (e.g. DIN5696.0)", spec)
    sys.exit(1)


def watch_attribute_command(args: argparse.Namespace) -> None:
    """Watch one or more CLU object attributes and print value changes."""
    config_path = get_config_path(args.config)

    attributes: list[tuple[str, str]] = []
    for spec in args.attribute:
        parsed = _parse_attribute(spec)
        if parsed not in attributes:
            attributes.append(parsed)

    manager = GrentonManager()
    try:
        asyncio.run(
            manager.watch_attributes(
                config_path,
                args.clu,
                attributes,
                args.output,
                args.refresh_interval,
            )
        )
    except KeyboardInterrupt:
        _LOGGER.info("Stopped watching")
