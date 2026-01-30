#!/usr/bin/env python3
"""CLI application for Grenton connection configuration."""
import logging
import sys
from pathlib import Path

# Setup path to import from custom_components
custom_components_path = Path(__file__).parent.parent.parent / "custom_components"
sys.path.insert(0, str(custom_components_path))

from commands import create_parser


def main() -> None:
    """Main entry point for CLI application."""
    parser = create_parser()
    args = parser.parse_args()
    
    # Configure logging based on debug flag
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
    )
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Dispatch to command handler
    args.handler(args)


if __name__ == "__main__":
    main()

