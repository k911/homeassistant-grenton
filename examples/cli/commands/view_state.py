"""View state command to display CLU state - garage door status example."""
import asyncio
import logging

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


def view_state_command(args) -> None:
    """Display garage door status using GrentonManager."""
    config_path = get_config_path(args.config)
    manager = GrentonManager()
    asyncio.run(manager.view_clu_state(config_path, args.clu))
