"""Verify shared-listener routing, renewals and remote cleanup on a live CLU."""

import argparse
import asyncio
import json

from config import get_config_path
from shared_subscription_probe import run_shared_subscription_test


def shared_subscription_command(args: argparse.Namespace) -> None:
    result = asyncio.run(
        run_shared_subscription_test(get_config_path(args.config), args.clu, args.duration)
    )
    print(json.dumps(result, indent=2))
    if result["result"] != "passed":
        raise SystemExit(1)


def observation_duration(value):
    duration = int(value)
    if not 100 <= duration <= 300:
        raise argparse.ArgumentTypeError("choose between 100 and 300 seconds")
    return duration
