"""Configure command implementation."""
import asyncio
import logging

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


def configure_command(args) -> None:
    """Execute configure command."""
    config_path = get_config_path(args.config)
    manager = GrentonManager()
    asyncio.run(manager.configure_and_test(args.url, args.pin, config_path))
