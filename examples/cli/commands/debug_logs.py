"""Enable CLU diagnostics and follow its Telnet log stream."""

import argparse
import asyncio
import logging
import sys
from contextlib import suppress

from clu_logs import LogOutputClosed
from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


def telnet_port(value: str) -> int:
    """Validate a TCP port before making any CLU changes."""
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def debug_logs_command(args: argparse.Namespace) -> None:
    """Follow logs until Ctrl+C, letting cancellation release both sockets."""
    try:
        asyncio.run(
            GrentonManager().debug_logs(
                get_config_path(args.config), args.clu, args.level, args.port,
            )
        )
    except KeyboardInterrupt:
        _LOGGER.info("Stopped listening to CLU logs")
    except LogOutputClosed:
        # Avoid a second broken-pipe error during interpreter shutdown flushing.
        with suppress(BrokenPipeError):
            sys.stdout.close()
