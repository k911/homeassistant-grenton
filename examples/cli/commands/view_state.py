"""View state command to display CLU state."""
import argparse
import asyncio
import json
import logging
import sys

import yaml

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


class QuotedDumper(yaml.SafeDumper):
    """Custom YAML dumper that quotes strings with spaces or special characters."""


def _represent_str(dumper, data: str) -> yaml.Node:
    """Represent strings, quoting those with spaces or special characters."""
    if any(char in data for char in (' ', ':', '-')):
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='"')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)


QuotedDumper.add_representer(str, _represent_str)


def _format_output(state_data: dict, output_format: str) -> str:
    """Format state data as JSON or YAML."""
    if output_format == "json":
        return json.dumps(state_data, indent=2, ensure_ascii=False)
    return yaml.dump(state_data, Dumper=QuotedDumper, default_flow_style=False, sort_keys=False, allow_unicode=True)


def view_state_command(args: argparse.Namespace) -> None:
    """Display CLU state with variables and attributes as JSON or YAML."""
    config_path = get_config_path(args.config)
    manager = GrentonManager()
    state_data = asyncio.run(manager.view_clu_state(config_path, args.clu))

    if state_data:
        output = _format_output(state_data, args.output)
        sys.stdout.write(output)
