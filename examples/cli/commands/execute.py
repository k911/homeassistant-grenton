"""Execute action command."""
import argparse
import asyncio
import logging
import sys

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


def execute_command(args: argparse.Namespace) -> None:
    """Execute action or script on CLU object."""
    if args.call_type in ("method", "attribute"):
        if args.action is None:
            _LOGGER.error("✗ --action is required when --call-type is %s", args.call_type)
            sys.exit(2)
        if args.value is None:
            _LOGGER.error("✗ --value is required when --call-type is %s", args.call_type)
            sys.exit(2)

    config_path = get_config_path(args.config)
    manager = GrentonManager()
    asyncio.run(
        manager.execute_action(
            config_path,
            args.clu,
            args.object,
            args.action,
            args.value or "",
            call_type=args.call_type,
        )
    )
