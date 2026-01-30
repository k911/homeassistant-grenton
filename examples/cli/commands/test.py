"""Test command implementation."""
import argparse
import asyncio
import logging

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


def test_command(args: argparse.Namespace) -> None:
    """Execute test command to verify CLU connections."""
    config_path = get_config_path(args.config)
    manager = GrentonManager()
    asyncio.run(manager.test_connection(config_path))
