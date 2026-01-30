"""Configure command implementation."""
import argparse
import asyncio
import logging

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


def configure_command(args: argparse.Namespace) -> None:
    """Execute configure command to fetch interface and test CLUs."""
    config_path = get_config_path(args.config)
    manager = GrentonManager()
    asyncio.run(manager.configure_and_test(args.url, args.pin, config_path))
