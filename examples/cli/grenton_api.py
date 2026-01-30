"""Grenton API integration."""
import logging
import sys
from pathlib import Path

custom_components_path = Path(__file__).parent.parent.parent / "custom_components"
sys.path.insert(0, str(custom_components_path))

from homeassistant_grenton.domain.api.clu import GrentonCluApi
from homeassistant_grenton.domain.api.object_manager import (
    GrentonObjectManagerApi,
    GrentonObjectManagerAuthError,
    GrentonObjectManagerConnectionError,
    GrentonObjectManagerDataError,
)
from homeassistant_grenton.domain.clu import GrentonClu
from homeassistant_grenton.domain.encryption import GrentonEncryption
from homeassistant_grenton.dto.clu import GrentonCluDto
from homeassistant_grenton.dto.encryption import GrentonEncryptionDto

from config import load_configuration, save_configuration, save_interface_cache

_LOGGER = logging.getLogger(__name__)


class GrentonManager:
    """Manages Grenton connections and operations."""

    async def configure_and_test(self, url: str, pin: str, config_path) -> None:
        """Configure connection and test CLUs."""
        try:
            _LOGGER.info("Connecting to Grenton Object Manager at %s", url)

            async with GrentonObjectManagerApi(url) as api:
                interface_data = await api.fetch_mobile_interface(pin)

            _LOGGER.info("✓ Successfully fetched mobile interface")
            _LOGGER.info("✓ Interface ID: %s", interface_data.get('id'))
            _LOGGER.info("✓ Interface name: %s", interface_data.get('name'))
            _LOGGER.info("✓ Version: %s", interface_data.get('version'))

            encryption, clus = self._create_objects(interface_data)
            save_configuration(interface_data, config_path)
            save_interface_cache(interface_data)

            await self._test_clus(clus, encryption)

        except GrentonObjectManagerAuthError:
            _LOGGER.error("✗ Authentication failed: Invalid PIN")
            sys.exit(1)
        except GrentonObjectManagerConnectionError as e:
            _LOGGER.error("✗ Connection error: %s", e)
            sys.exit(1)
        except GrentonObjectManagerDataError as e:
            _LOGGER.error("✗ Data error: %s", e)
            sys.exit(1)
        except Exception as e:
            _LOGGER.error("✗ Unexpected error: %s", e)
            raise

    async def test_connection(self, config_path) -> None:
        """Test connection using saved configuration."""
        config = load_configuration(config_path)
        if not config:
            sys.exit(1)

        await self._test_clus(config.clus, config.encryption)

    async def view_clu_state(self, config_path, clu_name: str) -> dict:
        """View CLU state with all attributes and variables using coordinator pattern.

        Returns a dict with CLU info, variables, and attributes grouped by label.
        """
        from homeassistant_grenton.state import GrentonState, GrentonCluState, GrentonCluStateVariableKey, GrentonCluStateAttributeKey

        config = load_configuration(config_path)
        if not config:
            sys.exit(1)

        # Use values from GrentonConfig
        encryption = config.encryption
        clus = config.clus
        cache_path = config.cache_path

        # Find CLU by name
        target_clu = next((c for c in clus if c.name == clu_name), None)
        if not target_clu:
            available = ", ".join([c.name for c in clus])
            _LOGGER.error("✗ CLU '%s' not found. Available CLUs: %s", clu_name, available)
            sys.exit(1)

        try:
            _LOGGER.info("Connecting to CLU '%s'", target_clu.name)

            api_client = GrentonCluApi(target_clu, encryption)
            if not await api_client.connect():
                _LOGGER.error("✗ Failed to connect to CLU")
                sys.exit(1)

            _LOGGER.info("✓ Connected successfully")

            # Initialize CLU state (using coordinator pattern)
            clu_state = GrentonCluState()

            # Parse the cached interface.json and merge tracked objects from config
            variable_labels, attribute_labels = self._parse_interface_cache_for_clu(cache_path, target_clu.id)
            tracked_by_clu = config.tracked_objects_by_clu.get(target_clu.name, {})
            tracked_variables = tracked_by_clu.get("variables", {})
            tracked_attributes = tracked_by_clu.get("attributes", {})
            self._merge_tracked_objects(
                variable_labels,
                attribute_labels,
                tracked_variables,
                tracked_attributes,
            )

            # Register discovered states into clu_state
            self._register_discovered_states(clu_state, variable_labels, attribute_labels)

            # Ping to keep alive
            await api_client.ping()

            # Register component states to get initial values
            _LOGGER.info("Fetching CLU state...")

            # Collect results grouped by label with detailed information
            result_vars_by_label: dict[str, list[dict]] = {}
            result_attrs_by_label: dict[str, list[dict]] = {}

            if clu_state.has_states_to_register():
                keys = clu_state.get_subscription_order()
                values = await api_client.register_component_states(keys)

                # Update state with returned values and collect grouped output
                if values:
                    for key, value in zip(keys, values):
                        if isinstance(key, GrentonCluStateVariableKey):
                            clu_state.set_variable(key, value)

                            labels = variable_labels.get(key.name) or [key.name]
                            item = {
                                "name": key.name,
                                "value": value,
                            }
                            # Add description if available in tracked objects
                            for tracked_vars in tracked_variables.values():
                                for var_info in tracked_vars:
                                    if isinstance(var_info, dict) and var_info.get("name") == key.name:
                                        if "description" in var_info:
                                            item["description"] = var_info["description"]
                                        break

                            for label in labels:
                                result_vars_by_label.setdefault(label, []).append(item)

                        elif isinstance(key, GrentonCluStateAttributeKey):
                            clu_state.set_attribute(key, value)

                            labels = attribute_labels.get((key.object_name, key.name)) or [f"{key.object_name}.{key.name}"]
                            item = {
                                "object": key.object_name,
                                "index": key.name,
                                "value": value,
                            }
                            # Add description if available in tracked objects
                            for tracked_attrs in tracked_attributes.values():
                                for attr_info in tracked_attrs:
                                    if isinstance(attr_info, dict) and attr_info.get("object") == key.object_name and attr_info.get("index") == key.name:
                                        if "description" in attr_info:
                                            item["description"] = attr_info["description"]
                                        break

                            for label in labels:
                                result_attrs_by_label.setdefault(label, []).append(item)

            else:
                _LOGGER.debug("[%s] No component states to register", target_clu.id)

            await api_client.disconnect()

            # Build output structure
            output = {
                "clu": {
                    "name": target_clu.name,
                    "id": target_clu.id,
                },
                "variables": result_vars_by_label if result_vars_by_label else {},
                "attributes": result_attrs_by_label if result_attrs_by_label else {},
            }

            _LOGGER.info("✓ State view completed successfully")
            return output

        except Exception as e:
            _LOGGER.error("✗ Error viewing CLU state: %s", e)
            sys.exit(1)

    def _parse_interface_cache_for_clu(self, cache_path: Path, clu_id: str) -> tuple[dict[str, list[str]], dict[tuple[str, str], list[str]]]:
        """Parse the cached interface JSON and return variable/attribute label maps for a CLU."""
        variable_labels: dict[str, list[str]] = {}
        attribute_labels: dict[tuple[str, str], list[str]] = {}

        if not cache_path.exists():
            _LOGGER.debug("Interface cache not found: %s", cache_path)
            return variable_labels, attribute_labels

        try:
            import json
            interface = json.loads(cache_path.read_text())

            discovered = []
            for page in interface.get("pages", []):
                for widget in page.get("widgets", []):
                    text = widget.get("text")
                    if text and text.get("callType") in ("VARIABLE", "ATTRIBUTE") and text.get("cluId") == clu_id:
                        discovered.append({
                            "label": widget.get("label") or text.get("label") or "",
                            "cluId": text.get("cluId"),
                            "objectName": text.get("objectName"),
                            "index": text.get("index"),
                            "callType": text.get("callType"),
                        })

                    for comp in widget.get("components", []):
                        state = comp.get("state")
                        if state and state.get("callType") in ("VARIABLE", "ATTRIBUTE") and state.get("cluId") == clu_id:
                            discovered.append({
                                "label": comp.get("label") or widget.get("label") or "",
                                "cluId": state.get("cluId"),
                                "objectName": state.get("objectName"),
                                "index": state.get("index"),
                                "callType": state.get("callType"),
                            })

            # Deduplicate by (callType, objectName, index, label)
            seen = set()
            unique = []
            for d in discovered:
                key = (d.get("callType"), d.get("objectName"), d.get("index"), d.get("label"))
                if key not in seen:
                    seen.add(key)
                    unique.append(d)

            for entry in unique:
                ctype = entry.get("callType")
                label = (entry.get("label") or "").strip()
                obj = entry.get("objectName") or entry.get("cluId")
                idx = entry.get("index")

                if ctype == "VARIABLE" and idx is not None:
                    idx = str(idx)
                    if label:
                        variable_labels.setdefault(idx, []).append(label)
                elif ctype == "ATTRIBUTE" and obj is not None and idx is not None:
                    obj = str(obj)
                    idx = str(idx)
                    if label:
                        attribute_labels.setdefault((obj, idx), []).append(label)

        except Exception as e:
            _LOGGER.error("Failed to parse interface cache: %s", e)

        return variable_labels, attribute_labels

    def _merge_tracked_objects(
        self,
        variable_labels: dict[str, list[str]],
        attribute_labels: dict[tuple[str, str], list[str]],
        tracked_variables: dict[str, list[dict]],
        tracked_attributes: dict[str, list[dict]],
    ) -> None:
        """Merge tracked objects from config.yaml into label maps."""
        for label, items in tracked_variables.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not name:
                    continue
                key = str(name)
                labels = variable_labels.setdefault(key, [])
                if label not in labels:
                    labels.append(label)

        for label, items in tracked_attributes.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                obj = item.get("object")
                idx = item.get("index")
                if obj is None or idx is None:
                    continue
                key = (str(obj), str(idx))
                labels = attribute_labels.setdefault(key, [])
                if label not in labels:
                    labels.append(label)

    def _register_discovered_states(
        self,
        clu_state: GrentonCluState,
        variable_labels: dict[str, list[str]],
        attribute_labels: dict[tuple[str, str], list[str]],
    ) -> None:
        """Add discovered variables/attributes to the CLU state object."""
        for idx in variable_labels.keys():
            clu_state.add_variable(idx)
        for obj_idx in attribute_labels.keys():
            obj, idx = obj_idx
            clu_state.add_attribute(obj, idx)

    async def _test_clus(self, clus: list[GrentonClu], encryption: GrentonEncryption) -> None:
        """Test CLU connections."""
        if not clus:
            _LOGGER.warning("No CLUs found")
            return

        print("\nConfigured CLUs:")
        print("-" * 70)
        for clu in clus:
            print(f"  Name: {clu.name}")
            print(f"  ID: {clu.id}")
            print(f"  Serial: {clu.serial_number}")
            print(f"  Address: {clu.ip}:{clu.port}\n")

        print("Testing CLU connections:")
        print("-" * 70)

        all_connected = True
        for clu in clus:
            try:
                api_client = GrentonCluApi(clu, encryption)
                connected = await api_client.connect()
                if connected:
                    pinged = await api_client.ping()
                    if pinged:
                        _LOGGER.info("✓ CLU '%s' connected and pinged successfully", clu.name)
                    else:
                        _LOGGER.error("✗ CLU '%s' ping failed", clu.name)
                        all_connected = False
                    await api_client.disconnect()
                else:
                    _LOGGER.error("✗ CLU '%s' connection failed", clu.name)
                    all_connected = False
            except Exception as e:
                _LOGGER.error("✗ CLU '%s' connection error: %s", clu.name, e)
                all_connected = False

        print("-" * 70)
        if all_connected:
            _LOGGER.info("✓ All CLUs connected successfully!")
            sys.exit(0)
        else:
            _LOGGER.error("✗ Some CLUs failed to connect")
            sys.exit(1)

    @staticmethod
    def _create_objects(interface_data: dict) -> tuple[GrentonEncryption, list[GrentonClu]]:
        """Create encryption and CLU objects from interface data."""
        encryption_dto = GrentonEncryptionDto(**interface_data.get("encryption", {}))
        encryption = GrentonEncryption.from_dto(encryption_dto)
        _LOGGER.info("✓ Encryption configured")

        clus_data = interface_data.get("clus", [])
        clus_dto = [GrentonCluDto(**clu_dict) for clu_dict in clus_data]
        clus = [GrentonClu.from_dto(clu_dto_obj) for clu_dto_obj in clus_dto]
        _LOGGER.info("✓ Loaded %d CLU(s)", len(clus))

        return encryption, clus
