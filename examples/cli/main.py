#!/usr/bin/env python3
"""CLI application for Grenton connection configuration."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

# Setup path to import from custom_components
custom_components_path = Path(__file__).parent.parent.parent / "custom_components"
sys.path.insert(0, str(custom_components_path))

# Import from API and domain modules
from homeassistant_grenton.domain.api.object_manager import (
    GrentonObjectManagerApi,
    GrentonObjectManagerAuthError,
    GrentonObjectManagerConnectionError,
    GrentonObjectManagerDataError,
)
from homeassistant_grenton.domain.api.clu import GrentonCluApi
from homeassistant_grenton.domain.encryption import GrentonEncryption
from homeassistant_grenton.domain.clu import GrentonClu
from homeassistant_grenton.dto.encryption import GrentonEncryptionDto
from homeassistant_grenton.dto.clu import GrentonCluDto


logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
_LOGGER = logging.getLogger(__name__)


def get_config_path(config_arg: Optional[str] = None) -> Path:
    """Get configuration file path from argument or default.
    
    Args:
        config_arg: Optional config path from command line
        
    Returns:
        Path to configuration file
    """
    if config_arg:
        return Path(config_arg).expanduser()
    return Path.home() / ".grenton" / "config.json"


def ensure_config_dir(config_path: Path) -> None:
    """Ensure configuration directory exists.
    
    Args:
        config_path: Path to configuration file
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)


def save_configuration(interface_data: dict, config_path: Path) -> None:
    """Save interface configuration to file.
    
    Args:
        interface_data: Raw interface data dictionary from API
        config_path: Path to configuration file
    """
    # Ensure directory exists
    ensure_config_dir(config_path)
    
    # Extract only necessary data (encryption and clus)
    config_data = {
        "id": interface_data.get("id"),
        "name": interface_data.get("name"),
        "version": interface_data.get("version"),
        "encryption": interface_data.get("encryption"),
        "clus": interface_data.get("clus"),
    }
    
    config_path.write_text(json.dumps(config_data, indent=2))
    _LOGGER.info(f"Configuration saved to {config_path}")


def load_configuration(config_path: Path) -> Optional[tuple[GrentonEncryption, list[GrentonClu]]]:
    """Load configuration from file.
    
    Args:
        config_path: Path to configuration file
        
    Returns:
        Tuple of (encryption, clus) if config exists, None otherwise
    """
    if not config_path.exists():
        return None
    
    try:
        config_data = json.loads(config_path.read_text())
        
        # Create DTO objects and convert to domain objects
        encryption_dto = GrentonEncryptionDto(**config_data["encryption"])
        clus_dto = [GrentonCluDto(**clu_dict) for clu_dict in config_data["clus"]]
        
        # Convert DTOs to domain objects
        encryption = GrentonEncryption.from_dto(encryption_dto)
        clus = [GrentonClu.from_dto(clu_dto) for clu_dto in clus_dto]
        
        _LOGGER.info(f"Configuration loaded from {config_path}")
        return encryption, clus
    except Exception as e:
        _LOGGER.error(f"Failed to load configuration: {e}")
        return None


async def configure_connection(url: str, pin: str, config_path: Path) -> None:
    """Configure Grenton connection and test CLUs.
    
    Args:
        url: Base URL of Grenton Object Manager
        pin: PIN for authentication
        config_path: Path to save configuration file
    """
    try:
        _LOGGER.info(f"Connecting to Grenton Object Manager at {url}")
        
        # Fetch mobile interface
        async with GrentonObjectManagerApi(url) as api:
            interface_data = await api.fetch_mobile_interface(pin)
        
        _LOGGER.info("✓ Successfully fetched mobile interface")
        
        # Extract data from raw interface
        _LOGGER.info(f"✓ Interface ID: {interface_data.get('id')}")
        _LOGGER.info(f"✓ Interface name: {interface_data.get('name')}")
        _LOGGER.info(f"✓ Version: {interface_data.get('version')}")
        
        # Create DTO objects and convert to domain objects
        encryption_dto = GrentonEncryptionDto(**interface_data.get("encryption", {}))
        encryption = GrentonEncryption.from_dto(encryption_dto)
        _LOGGER.info("✓ Encryption configured")
        
        # Create CLU domain objects
        clus_data = interface_data.get("clus", [])
        clus_dto = [GrentonCluDto(**clu_dict) for clu_dict in clus_data]
        clus = [GrentonClu.from_dto(clu_dto_obj) for clu_dto_obj in clus_dto]
        _LOGGER.info(f"✓ Loaded {len(clus)} CLU(s)")
        
        # Save configuration for future use
        save_configuration(interface_data, config_path)
        
        if not clus:
            _LOGGER.warning("No CLUs found in interface")
            return
        
        # Display CLU information
        print("\nConfigured CLUs:")
        print("-" * 70)
        for clu in clus:
            print(f"  Name: {clu.name}")
            print(f"  ID: {clu.id}")
            print(f"  Serial: {clu.serial_number}")
            print(f"  Address: {clu.ip}:{clu.port}")
            print()
        
        # Test connection to each CLU
        print("Testing CLU connections:")
        print("-" * 70)
        
        all_connected = True
        for clu in clus:
            try:
                api_client = GrentonCluApi(clu, encryption)
                connected = await api_client.connect()
                pinged = await api_client.ping()
                if connected and pinged:
                    _LOGGER.info(f"✓ CLU '{clu.name}' connected and pinged successfully")
                    await api_client.disconnect()
                else:
                    _LOGGER.error(f"✗ CLU '{clu.name}' connection or ping failed")
                    all_connected = False
            except Exception as e:
                _LOGGER.error(f"✗ CLU '{clu.name}' connection error: {e}")
                all_connected = False
        
        # Final status
        print("-" * 70)
        if all_connected and clus:
            _LOGGER.info("✓ All CLUs connected successfully!")
            sys.exit(0)
        else:
            _LOGGER.error("✗ Some CLUs failed to connect")
            sys.exit(1)
    
    except GrentonObjectManagerAuthError:
        _LOGGER.error("✗ Authentication failed: Invalid PIN")
        sys.exit(1)
    except GrentonObjectManagerConnectionError as e:
        _LOGGER.error(f"✗ Connection error: {e}")
        sys.exit(1)
    except GrentonObjectManagerDataError as e:
        _LOGGER.error(f"✗ Data error: {e}")
        sys.exit(1)
    except Exception as e:
        _LOGGER.error(f"✗ Unexpected error: {e}")
        sys.exit(1)


def main() -> None:
    """Main entry point for CLI application."""
    parser = argparse.ArgumentParser(
        description="Grenton connection configuration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s configure --pin 1234
  %(prog)s configure --url http://192.168.1.100:9998 --pin 5678
        """,
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Configure command
    configure_parser = subparsers.add_parser(
        "configure",
        help="Configure Grenton connection",
    )
    configure_parser.add_argument(
        "--url",
        default="http://localhost:9998",
        help="Grenton Object Manager URL (default: http://localhost:9998)",
    )
    configure_parser.add_argument(
        "--pin",
        required=True,
        help="Grenton Object Manager PIN",
    )
    configure_parser.add_argument(
        "--config",
        default=None,
        help="Configuration file path (default: ~/.grenton/config.json)",
    )
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    if args.command == "configure":
        config_path = get_config_path(args.config)
        asyncio.run(configure_connection(args.url, args.pin, config_path))


if __name__ == "__main__":
    main()
