"""CLI commands package."""
import argparse

from .configure import configure_command
from .test import test_command
from .view_state import view_state_command


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
  %(prog)s test --config ~/.grenton/config.json
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
        help="Configuration file path (default: ~/.grenton/config.json)",
    )
    configure_parser.set_defaults(handler=configure_command)
    
    # Test command
    test_parser = subparsers.add_parser("test", help="Test Grenton connection")
    test_parser.add_argument(
        "--config",
        default=None,
        help="Configuration file path (default: ~/.grenton/config.json)",
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
        help="Configuration file path (default: ~/.grenton/config.json)",
    )
    view_state_parser.set_defaults(handler=view_state_command)
    
    return parser
