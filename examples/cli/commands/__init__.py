"""CLI commands package."""
import argparse

from .configure import configure_command
from .test import test_command
from .view_state import view_state_command
from .execute import execute_command
from .watch_variable import watch_variable_command


def create_parser() -> argparse.ArgumentParser:
    """Create and return the argument parser."""
    parser = argparse.ArgumentParser(
        description="Grenton connection configuration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s configure --pin 1234
  %(prog)s configure --url http://192.168.1.100:9998 --pin 5678
  %(prog)s test
  %(prog)s test --config ~/.grenton/config.yaml
  %(prog)s watch-variable --variable myVariable
  %(prog)s watch-variable --clu CLU_Z_WAVE_1 --variable myVariable --variable otherVariable
        """,
    )

    # Global options
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Configure command
    configure_parser = subparsers.add_parser("configure", help="Configure Grenton connection")
    configure_parser.add_argument(
        "--url",
        default="http://localhost:9998",
        help="Grenton Object Manager URL (default: http://localhost:9998)",
    )
    configure_parser.add_argument("--pin", required=True, help="Grenton Object Manager PIN")
    configure_parser.add_argument(
        "--config",
        default=None,
        help="Configuration file path (default: ~/.grenton/config.yaml)",
    )
    configure_parser.set_defaults(handler=configure_command)

    # Test command
    test_parser = subparsers.add_parser("test", help="Test Grenton connection")
    test_parser.add_argument(
        "--config",
        default=None,
        help="Configuration file path (default: ~/.grenton/config.yaml)",
    )
    test_parser.set_defaults(handler=test_command)

    # View state command
    view_state_parser = subparsers.add_parser("view-state", help="Display CLU state and attributes")
    view_state_parser.add_argument(
        "--clu",
        default="CLU_Z_WAVE_1",
        help="CLU name to view state for (default: CLU_Z_WAVE_1)",
    )
    view_state_parser.add_argument(
        "--config",
        default=None,
        help="Configuration file path (default: ~/.grenton/config.yaml)",
    )
    view_state_parser.add_argument(
        "--output",
        choices=["yaml", "json"],
        default="yaml",
        help="Output format: yaml or json (default: yaml)",
    )
    view_state_parser.set_defaults(handler=view_state_command)

    # Execute action command
    execute_parser = subparsers.add_parser("execute", help="Execute action on CLU object")
    execute_parser.add_argument(
        "--clu",
        default="CLU_Z_WAVE_1",
        help="CLU name (default: CLU_Z_WAVE_1)",
    )
    execute_parser.add_argument(
        "--object",
        required=True,
        help="Object ID/name (e.g., DOU0699)",
    )
    execute_parser.add_argument(
        "--action",
        required=True,
        help="Action index/name (e.g., set, toggle, turnOn)",
    )
    execute_parser.add_argument(
        "--value",
        required=True,
        help="Action value (e.g., 1, 0, or command)",
    )
    execute_parser.add_argument(
        "--config",
        default=None,
        help="Configuration file path (default: ~/.grenton/config.yaml)",
    )
    execute_parser.set_defaults(handler=execute_command)

    # Watch variable command
    watch_parser = subparsers.add_parser(
        "watch-variable",
        help="Subscribe to CLU variables and print value changes",
    )
    watch_parser.add_argument(
        "--clu",
        default="CLU_Z_WAVE_1",
        help="CLU name (default: CLU_Z_WAVE_1)",
    )
    watch_parser.add_argument(
        "--variable",
        action="append",
        required=True,
        metavar="NAME",
        help="Variable name to watch (repeat for multiple variables)",
    )
    watch_parser.add_argument(
        "--config",
        default=None,
        help="Configuration file path (default: ~/.grenton/config.yaml)",
    )
    watch_parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format: text or json lines (default: text)",
    )
    watch_parser.add_argument(
        "--refresh-interval",
        type=int,
        default=45,
        help="Seconds between subscription refreshes (default: 45)",
    )
    watch_parser.set_defaults(handler=watch_variable_command)
    return parser
