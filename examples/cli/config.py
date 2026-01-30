"""Configuration management utilities."""
import json
import logging
from pathlib import Path
from typing import Optional

import sys
import yaml
custom_components_path = Path(__file__).parent.parent.parent / "custom_components"
sys.path.insert(0, str(custom_components_path))

from homeassistant_grenton.domain.clu import GrentonClu
from homeassistant_grenton.domain.encryption import GrentonEncryption
from homeassistant_grenton.dto.clu import GrentonCluDto
from homeassistant_grenton.dto.encryption import GrentonEncryptionDto

_LOGGER = logging.getLogger(__name__)


def get_config_path(config_arg: Optional[str] = None) -> Path:
    """Get configuration file path from argument or default (YAML)."""
    if config_arg:
        return Path(config_arg).expanduser()
    return Path.home() / ".grenton" / "config.yaml"


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
    if cache_path is None:
        cache_path = Path.home() / ".grenton" / "cache" / "interface.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cache_path.write_text(json.dumps(interface_data, indent=2))
    _LOGGER.info(f"Interface cache saved to {cache_path}")


from dataclasses import dataclass, field


@dataclass
class GrentonConfig:
    encryption: GrentonEncryption
    clus: list[GrentonClu]
    cache_path: Path
    # Tracked objects provided in config.yaml under 'grenton_tracked_objects'
    # {clu_name: {variables: {label: [..]}, attributes: {label: [..]}}}
    tracked_objects_by_clu: dict[str, dict[str, dict[str, list[dict]]]] = field(default_factory=dict)


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

        # Resolve cache path if provided in YAML
        cache_path_value = None
        if isinstance(config_data.get("cache"), dict):
            cache_path_value = config_data["cache"].get("interface")
        if not cache_path_value:
            cache_path_value = config_data.get("cache_path")

        if cache_path_value:
            cache_path = Path(cache_path_value).expanduser()
        else:
            cache_path = Path.home() / ".grenton" / "cache" / "interface.json"

        tracked_objects_by_clu: dict[str, dict[str, dict[str, list[dict]]]] = {}
        tracked_objects = config_data.get("grenton_tracked_objects", {})
        if isinstance(tracked_objects, dict):
            for clu_name, clu_section in tracked_objects.items():
                if not isinstance(clu_section, dict):
                    continue
                clu_key = str(clu_name)
                variables_raw = clu_section.get("variables") if isinstance(clu_section, dict) else {}
                attributes_raw = clu_section.get("attributes") if isinstance(clu_section, dict) else {}

                tracked_variables: dict[str, list[dict]] = {}
                tracked_attributes: dict[str, list[dict]] = {}

                if isinstance(variables_raw, dict):
                    for label, items in variables_raw.items():
                        if isinstance(items, list):
                            tracked_variables[str(label)] = [item for item in items if isinstance(item, dict)]

                if isinstance(attributes_raw, dict):
                    for label, items in attributes_raw.items():
                        if isinstance(items, list):
                            tracked_attributes[str(label)] = [item for item in items if isinstance(item, dict)]

                tracked_objects_by_clu[clu_key] = {
                    "variables": tracked_variables,
                    "attributes": tracked_attributes,
                }

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
