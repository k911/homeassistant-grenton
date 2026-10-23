"""Watch variable command to follow CLU variable value changes."""
import argparse
import asyncio
import logging

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


def watch_variable_command(args: argparse.Namespace) -> None:
    """Watch one or more CLU variables and print value changes."""
    config_path = get_config_path(args.config)

    variables: list[str] = []
    for spec in args.variable:
        name = spec.strip()
        if name and name not in variables:
            variables.append(name)

    manager = GrentonManager()
    try:
        asyncio.run(
            manager.watch_variables(
                config_path,
                args.clu,
                variables,
                args.output,
                args.refresh_interval,
            )
        )
    except KeyboardInterrupt:
        _LOGGER.info("Stopped watching")
