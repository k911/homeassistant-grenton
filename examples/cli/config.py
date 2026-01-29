"""Configuration management utilities."""
import json
import logging
from pathlib import Path
from typing import Optional

import sys
custom_components_path = Path(__file__).parent.parent.parent / "custom_components"
sys.path.insert(0, str(custom_components_path))

from homeassistant_grenton.domain.clu import GrentonClu
from homeassistant_grenton.domain.encryption import GrentonEncryption
from homeassistant_grenton.dto.clu import GrentonCluDto
from homeassistant_grenton.dto.encryption import GrentonEncryptionDto

_LOGGER = logging.getLogger(__name__)


def get_config_path(config_arg: Optional[str] = None) -> Path:
    """Get configuration file path from argument or default."""
    if config_arg:
        return Path(config_arg).expanduser()
    return Path.home() / ".grenton" / "config.json"


def ensure_config_dir(config_path: Path) -> None:
    """Ensure configuration directory exists."""
    config_path.parent.mkdir(parents=True, exist_ok=True)


def save_configuration(interface_data: dict, config_path: Path) -> None:
    """Save interface configuration to file."""
    ensure_config_dir(config_path)
    
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
    """Load configuration from file."""
    if not config_path.exists():
        _LOGGER.error(f"Configuration file not found: {config_path}")
        return None
    
    try:
        config_data = json.loads(config_path.read_text())
        
        encryption_dto = GrentonEncryptionDto(**config_data["encryption"])
        clus_dto = [GrentonCluDto(**clu_dict) for clu_dict in config_data["clus"]]
        
        encryption = GrentonEncryption.from_dto(encryption_dto)
        clus = [GrentonClu.from_dto(clu_dto) for clu_dto in clus_dto]
        
        _LOGGER.info(f"Configuration loaded from {config_path}")
        return encryption, clus
    except Exception as e:
        _LOGGER.error(f"Failed to load configuration: {e}")
        return None
