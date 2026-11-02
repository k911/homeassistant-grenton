"""Run a bounded subscription cleanup test without changing object values."""

import argparse
import asyncio
import json

from config import get_config_path
from subscription_probe import run_client_destroy_test


def observation_seconds(value: str) -> int:
    seconds = int(value)
    if not 5 <= seconds <= 15:
        raise argparse.ArgumentTypeError("choose between 5 and 15 seconds")
    return seconds


def client_destroy_command(args: argparse.Namespace) -> None:
    result = asyncio.run(
        run_client_destroy_test(get_config_path(args.config), args.clu, args.observe_seconds)
    )
    print(json.dumps(result, indent=2))
    if result["result"] != "passed":
        raise SystemExit(1)
