"""CLI commands package."""
import argparse

from .configure import configure_command
from .test import test_command


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
    
    return parser
