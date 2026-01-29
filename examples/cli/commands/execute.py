"""Execute action command."""
import argparse
import asyncio
import logging

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


def execute_command(args: argparse.Namespace) -> None:
    """Execute action on CLU object."""
    config_path = get_config_path(args.config)
    manager = GrentonManager()
    asyncio.run(
        manager.execute_action(
            config_path,
            args.clu,
            args.object,
            args.action,
            args.value,
        )
    )
