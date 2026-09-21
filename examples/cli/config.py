"""Configuration management utilities."""
import json
import logging
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
_INTERFACE_CONFIG_KEYS = ("id", "name", "version")


def get_config_path(config_arg: Optional[str] = None) -> Path:
    """Get configuration file path from argument or default."""
    return Path(config_arg).expanduser() if config_arg else _DEFAULT_CONFIG_PATH


def ensure_config_dir(config_path: Path) -> None:
    """Ensure configuration directory exists."""
    config_path.parent.mkdir(parents=True, exist_ok=True)


def save_configuration(
    interface_data: dict,
    config_path: Path,
    update_tracked_objects: bool = False,
) -> None:
    """Merge interface configuration into the YAML file.

    Existing keys and custom fields are retained. Tracked objects are only
    discovered and merged when ``update_tracked_objects`` is requested.
    """
    ensure_config_dir(config_path)

    config_data = _load_yaml_mapping(config_path)
    for key in _INTERFACE_CONFIG_KEYS:
        if key in interface_data:
            config_data[key] = deepcopy(interface_data[key])

    _merge_mapping_field(config_data, "encryption", interface_data.get("encryption"))
    _merge_clus(config_data, interface_data.get("clus"))

    if update_tracked_objects:
        discovered = discover_tracked_objects(interface_data)
        _merge_discovered_tracked_objects(config_data, discovered)

    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False, allow_unicode=True))
    _LOGGER.info(f"Configuration saved to {config_path}")


def _load_yaml_mapping(config_path: Path) -> dict:
    """Load an existing YAML mapping without discarding custom configuration."""
    if not config_path.exists():
        return {}

    loaded = yaml.safe_load(config_path.read_text())
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration root must be a mapping: {config_path}")
    return loaded


def _merge_mapping_field(config_data: dict, key: str, incoming: Any) -> None:
    """Update a mapping field while retaining existing custom keys."""
    if not isinstance(incoming, dict):
        return

    existing = config_data.get(key)
    if isinstance(existing, dict):
        existing.update(deepcopy(incoming))
    else:
        config_data[key] = deepcopy(incoming)


def _merge_clus(config_data: dict, incoming: Any) -> None:
    """Merge CLUs by ID (or name as a fallback), preserving custom fields."""
    if not isinstance(incoming, list):
        return

    existing = config_data.get("clus")
    if not isinstance(existing, list):
        config_data["clus"] = deepcopy(incoming)
        return

    by_id = {
        str(clu["id"]): clu
        for clu in existing
        if isinstance(clu, dict) and clu.get("id") is not None
    }
    by_name = {
        str(clu["name"]): clu
        for clu in existing
        if isinstance(clu, dict) and clu.get("name") is not None
    }
    for incoming_clu in incoming:
        if not isinstance(incoming_clu, dict):
            continue
        target = None
        if incoming_clu.get("id") is not None:
            target = by_id.get(str(incoming_clu["id"]))
        if target is None and incoming_clu.get("name") is not None:
            target = by_name.get(str(incoming_clu["name"]))
        if target is None:
            target = deepcopy(incoming_clu)
            existing.append(target)
        else:
            target.update(deepcopy(incoming_clu))

        if target.get("id") is not None:
            by_id[str(target["id"])] = target
        if target.get("name") is not None:
            by_name[str(target["name"])] = target


def discover_tracked_objects(interface_data: dict) -> dict[str, dict[str, list[tuple[str, dict]]]]:
    """Discover every unique variable and attribute referenced by the interface.

    Results retain interface order so a new item uses the first label that
    references it. A nested component label takes precedence over its parent
    widget label.
    """
    clu_names = {
        str(clu["id"]): str(clu["name"])
        for clu in interface_data.get("clus", [])
        if isinstance(clu, dict) and clu.get("id") is not None and clu.get("name") is not None
    }
    result: dict[str, dict[str, list[tuple[str, dict]]]] = {}
    seen_variables: set[tuple[str, str]] = set()
    seen_attributes: dict[tuple[str, str], dict] = {}

    def visit(value: Any, inherited_label: str = "", field_name: Optional[str] = None) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, inherited_label)
            return
        if not isinstance(value, dict):
            return

        label = str(value.get("label") or inherited_label).strip()
        call_type = value.get("callType")
        clu_id = value.get("cluId")
        clu_name = clu_names.get(str(clu_id)) if clu_id is not None else None
        index = value.get("index")

        if clu_name and call_type == "VARIABLE" and index is not None:
            name = str(index)
            identity = (clu_name, name)
            if identity not in seen_variables:
                seen_variables.add(identity)
                group = label or name
                section = result.setdefault(clu_name, {"variables": [], "attributes": []})
                section["variables"].append((group, {"name": name}))
        elif clu_name and call_type == "ATTRIBUTE" and value.get("objectName") is not None and index is not None:
            object_id = str(value["objectName"])
            index_value = _numeric_index(index)
            identity = (object_id, str(index_value))
            existing_item = seen_attributes.get(identity)
            if existing_item is None:
                group = label or f"{object_id}.{index_value}"
                section = result.setdefault(clu_name, {"variables": [], "attributes": []})
                item = {"object": object_id, "index": index_value}
                if field_name:
                    item["_source_name"] = field_name
                section["attributes"].append((group, item))
                seen_attributes[identity] = item
            elif (
                field_name
                and str(existing_item.get("_source_name", "")).endswith("Action")
                and not field_name.endswith("Action")
            ):
                # State fields describe an attribute better than an action
                # field that happens to write the same object and index.
                existing_item["_source_name"] = field_name

        for child_name, child in value.items():
            if isinstance(child, (dict, list)):
                visit(child, label, str(child_name))

    for page in interface_data.get("pages", []):
        if not isinstance(page, dict):
            continue
        for widget in page.get("widgets", []):
            visit(widget)

    for sections in result.values():
        label_counts: dict[str, int] = {}
        for label, _item in sections["attributes"]:
            label_counts[label] = label_counts.get(label, 0) + 1
        for label, item in sections["attributes"]:
            source_name = item.pop("_source_name", None)
            if label_counts[label] > 1 and source_name:
                item["description"] = source_name

    return result


def _numeric_index(value: Any) -> int:
    """Return an attribute index as the integer represented in the interface."""
    if isinstance(value, bool):
        raise ValueError(f"Attribute index must be numeric, got {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"Attribute index must be an integer, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Attribute index must be numeric, got {value!r}") from error


def _merge_discovered_tracked_objects(config_data: dict, discovered: dict) -> None:
    """Add discovered items without moving or changing existing custom items."""
    tracked = config_data.get("grenton_tracked_objects")
    if tracked is None:
        tracked = {}
        config_data["grenton_tracked_objects"] = tracked
    if not isinstance(tracked, dict):
        raise ValueError("grenton_tracked_objects must be a mapping")

    existing_variables: set[tuple[str, str]] = set()
    existing_attributes: dict[tuple[str, str], dict] = {}
    for existing_clu_name, existing_clu_section in tracked.items():
        if not isinstance(existing_clu_section, dict):
            continue
        variables = existing_clu_section.get("variables", {})
        if isinstance(variables, dict):
            for items in variables.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict) and item.get("name") is not None:
                        existing_variables.add((str(existing_clu_name), str(item["name"])))
        attributes = existing_clu_section.get("attributes", {})
        if isinstance(attributes, dict):
            for items in attributes.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if (
                        isinstance(item, dict)
                        and item.get("object") is not None
                        and item.get("index") is not None
                    ):
                        item["index"] = _numeric_index(item["index"])
                        identity = (str(item["object"]), str(item["index"]))
                        existing_attributes.setdefault(identity, item)

    for clu_name, discovered_sections in discovered.items():
        clu_section = tracked.setdefault(clu_name, {})
        if not isinstance(clu_section, dict):
            raise ValueError(f"grenton_tracked_objects.{clu_name} must be a mapping")

        for section_name in ("variables", "attributes"):
            section = clu_section.setdefault(section_name, {})
            if not isinstance(section, dict):
                raise ValueError(
                    f"grenton_tracked_objects.{clu_name}.{section_name} must be a mapping"
                )

            for label, item in discovered_sections[section_name]:
                if section_name == "variables":
                    identity = (clu_name, str(item["name"]))
                    identities = existing_variables
                    existing_item = None
                else:
                    identity = (str(item["object"]), str(item["index"]))
                    identities = existing_attributes
                    existing_item = existing_attributes.get(identity)
                if identity in identities:
                    if existing_item is not None and item.get("description"):
                        existing_item.setdefault("description", item["description"])
                    continue
                items = section.setdefault(label, [])
                if not isinstance(items, list):
                    raise ValueError(
                        f"grenton_tracked_objects.{clu_name}.{section_name}.{label} must be a list"
                    )
                items.append(item)
                if section_name == "variables":
                    identities.add(identity)
                else:
                    identities[identity] = item


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
