"""Configuration management utilities."""
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

custom_components_path = Path(__file__).parent.parent.parent / "custom_components"
sys.path.insert(0, str(custom_components_path))

from homeassistant_grenton.domain.clu import GrentonClu
from homeassistant_grenton.domain.encryption import GrentonEncryption
from homeassistant_grenton.dto.clu import GrentonCluDto
from homeassistant_grenton.dto.encryption import GrentonEncryptionDto

_LOGGER = logging.getLogger(__name__)
_DEFAULT_CACHE_PATH = Path.home() / ".grenton" / "cache" / "interface.json"
_DEFAULT_CONFIG_PATH = Path.home() / ".grenton" / "config.yaml"


def get_config_path(config_arg: Optional[str] = None) -> Path:
    """Get configuration file path from argument or default."""
    return Path(config_arg).expanduser() if config_arg else _DEFAULT_CONFIG_PATH


def ensure_config_dir(config_path: Path) -> None:
    """Ensure configuration directory exists."""
    config_path.parent.mkdir(parents=True, exist_ok=True)


def save_configuration(interface_data: dict, config_path: Path) -> None:
    """Save interface configuration to YAML file."""
    ensure_config_dir(config_path)

    config_data = {
        "id": interface_data.get("id"),
        "name": interface_data.get("name"),
        "version": interface_data.get("version"),
        "encryption": interface_data.get("encryption"),
        "clus": interface_data.get("clus"),
    }

    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False))
    _LOGGER.info(f"Configuration saved to {config_path}")


def save_interface_cache(interface_data: dict, cache_path: Optional[Path] = None) -> None:
    """Save full mobile interface data to cache for analysis (JSON by spec)."""
    cache_path = cache_path or _DEFAULT_CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(interface_data, indent=2))
    _LOGGER.info(f"Interface cache saved to {cache_path}")


@dataclass
class GrentonConfig:
    """Configuration data container with tracked objects per CLU."""

    encryption: GrentonEncryption
    clus: list[GrentonClu]
    cache_path: Path
    tracked_objects_by_clu: dict[str, dict[str, dict[str, list[dict]]]] = field(default_factory=dict)


def _parse_tracked_objects(tracked_objects: dict) -> dict[str, dict[str, dict[str, list[dict]]]]:
    """Parse tracked objects from config YAML structure (per-CLU)."""
    result: dict[str, dict[str, dict[str, list[dict]]]] = {}
    if not isinstance(tracked_objects, dict):
        return result

    for clu_name, clu_section in tracked_objects.items():
        if not isinstance(clu_section, dict):
            continue

        variables = _extract_labeled_items(clu_section.get("variables", {}))
        attributes = _extract_labeled_items(clu_section.get("attributes", {}))

        result[str(clu_name)] = {"variables": variables, "attributes": attributes}

    return result


def _extract_labeled_items(labeled_section: dict) -> dict[str, list[dict]]:
    """Extract items from a labeled section (e.g., 'variables', 'attributes')."""
    result: dict[str, list[dict]] = {}
    if not isinstance(labeled_section, dict):
        return result

    for label, items in labeled_section.items():
        if isinstance(items, list):
            result[str(label)] = [item for item in items if isinstance(item, dict)]

    return result


def load_configuration(config_path: Path) -> Optional[GrentonConfig]:
    """Load configuration from YAML file and return a GrentonConfig object.

    The config YAML may include either a top-level 'cache_path' field or a 'cache' mapping with
    'interface' key. If not present, the default cache path is '~/.grenton/cache/interface.json'.
    """
    if not config_path.exists():
        _LOGGER.error(f"Configuration file not found: {config_path}")
        return None

    try:
        config_data = yaml.safe_load(config_path.read_text()) or {}

        encryption_dto = GrentonEncryptionDto(**config_data.get("encryption", {}))
        clus_dto = [GrentonCluDto(**clu_dict) for clu_dict in config_data.get("clus", [])]

        encryption = GrentonEncryption.from_dto(encryption_dto)
        clus = [GrentonClu.from_dto(clu_dto) for clu_dto in clus_dto]

        # Resolve cache path from config or use default
        cache_value = config_data.get("cache", {}).get("interface") if isinstance(config_data.get("cache"), dict) else None
        cache_value = cache_value or config_data.get("cache_path")
        cache_path = Path(cache_value).expanduser() if cache_value else _DEFAULT_CACHE_PATH

        # Parse tracked objects from config (per-CLU structure)
        tracked_objects_by_clu = _parse_tracked_objects(config_data.get("grenton_tracked_objects", {}))

        _LOGGER.info(f"Configuration loaded from {config_path}")
        return GrentonConfig(
            encryption=encryption,
            clus=clus,
            cache_path=cache_path,
            tracked_objects_by_clu=tracked_objects_by_clu,
        )
    except Exception as e:
        _LOGGER.error(f"Failed to load configuration: {e}")
        return None
