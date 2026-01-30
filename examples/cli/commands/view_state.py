"""View state command to display CLU state - garage door status example."""
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
    pass


def represent_str(dumper, data):
    """Represent strings, quoting those with spaces or special characters."""
    if ' ' in data or ':' in data or '-' in data:
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='"')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)


QuotedDumper.add_representer(str, represent_str)


def view_state_command(args) -> None:
    """Display CLU state using GrentonManager."""
    config_path = get_config_path(args.config)
    manager = GrentonManager()
    state_data = asyncio.run(manager.view_clu_state(config_path, args.clu))

    if state_data:
        if args.output == "json":
            output = json.dumps(state_data, indent=2, ensure_ascii=False)
        else:  # yaml
            output = yaml.dump(state_data, Dumper=QuotedDumper, default_flow_style=False, sort_keys=False, allow_unicode=True)
        sys.stdout.write(output)
