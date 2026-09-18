"""Grenton API integration."""
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

custom_components_path = Path(__file__).parent.parent.parent / "custom_components"
sys.path.insert(0, str(custom_components_path))

from homeassistant_grenton.domain.api.clu import GrentonCluApi
from homeassistant_grenton.domain.api.clu_cli import GrentonCluCliApi
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

_STATE_TYPES = ("VARIABLE", "ATTRIBUTE")
_PING_INTERVAL = 5
_ERROR_MESSAGES = {
    GrentonObjectManagerAuthError: "✗ Authentication failed: Invalid PIN",
    GrentonObjectManagerConnectionError: "✗ Connection error: %s",
    GrentonObjectManagerDataError: "✗ Data error: %s",
}


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
        except (GrentonObjectManagerConnectionError, GrentonObjectManagerDataError) as e:
            _LOGGER.error(_ERROR_MESSAGES.get(type(e), "✗ Error: %s"), str(e) if str(e) else "")
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

    async def execute_action(self, config_path: Path, clu_name: str, object_name: str, action_index: str, action_value: str) -> None:
        """Execute action on CLU object.

        Creates a GrentonActionMethod message: object_name:execute(action_index, action_value)
        """
        from homeassistant_grenton.domain.action import GrentonActionMethod
        from homeassistant_grenton.domain.enums import GrentonActionEventType

        config = load_configuration(config_path)
        if not config:
            sys.exit(1)

        # Find CLU by name
        target_clu = next((c for c in config.clus if c.name == clu_name), None)
        if not target_clu:
            available = ", ".join([c.name for c in config.clus])
            _LOGGER.error("✗ CLU '%s' not found. Available CLUs: %s", clu_name, available)
            sys.exit(1)

        try:
            _LOGGER.info("Connecting to CLU '%s'", target_clu.name)

            api_client = GrentonCluApi(target_clu, config.encryption)
            if not await api_client.connect():
                _LOGGER.error("✗ Failed to connect to CLU")
                sys.exit(1)

            _LOGGER.info("✓ Connected successfully")

            # Create and execute action
            _LOGGER.info("Executing: %s.execute(%s, %s)", object_name, action_index, action_value)

            action = GrentonActionMethod(
                clu_id=target_clu.id,
                object_name=object_name,
                event=GrentonActionEventType.CLICK,
                index=action_index,
                value=action_value,
            )

            result = await api_client.execute_action(action)

            await api_client.disconnect()

            if result:
                _LOGGER.info("✓ Action executed successfully")
                print(f"✓ {object_name}.execute({action_index}, \"{action_value}\")")
                sys.exit(0)
            else:
                _LOGGER.error("✗ Action execution failed")
                sys.exit(1)

        except Exception as e:
            _LOGGER.error("✗ Error executing action: %s", e)
            sys.exit(1)

    async def watch_variables(
        self,
        config_path: Path,
        clu_name: str,
        variables: list[str],
        output_format: str = "text",
        refresh_interval: int = 45,
    ) -> None:
        """Subscribe to CLU variables and print every value change until interrupted."""
        from homeassistant_grenton.state import GrentonCluStateVariableKey

        keys = [GrentonCluStateVariableKey(name) for name in variables]
        await self._watch_states(config_path, clu_name, keys, "variable", output_format, refresh_interval)

    async def watch_attributes(
        self,
        config_path: Path,
        clu_name: str,
        attributes: list[tuple[str, str]],
        output_format: str = "text",
        refresh_interval: int = 45,
    ) -> None:
        """Subscribe to CLU object attributes and print every value change until interrupted."""
        from homeassistant_grenton.state import GrentonCluStateAttributeKey

        keys = [GrentonCluStateAttributeKey(obj, index) for obj, index in attributes]
        await self._watch_states(config_path, clu_name, keys, "attribute", output_format, refresh_interval)

    async def _watch_states(
        self,
        config_path: Path,
        clu_name: str,
        keys: list,
        kind: str,
        output_format: str,
        refresh_interval: int,
    ) -> None:
        """Subscribe to CLU state keys and print every value change until interrupted."""
        from homeassistant_grenton.state import GrentonCluStateAttributeKey, GrentonValue

        config = load_configuration(config_path)
        if not config:
            sys.exit(1)

        target_clu = next((c for c in config.clus if c.name == clu_name), None)
        if not target_clu:
            available = ", ".join([c.name for c in config.clus])
            _LOGGER.error("✗ CLU '%s' not found. Available CLUs: %s", clu_name, available)
            sys.exit(1)

        last_values: dict[str, GrentonValue] = {}

        def label_for_key(key) -> str:
            if isinstance(key, GrentonCluStateAttributeKey):
                return f"{key.object_name}.{key.name}"
            return key.name

        def emit(label: str, value: GrentonValue) -> None:
            timestamp = datetime.now().isoformat(timespec="seconds")
            if output_format == "json":
                line = json.dumps(
                    {"timestamp": timestamp, "clu": target_clu.name, kind: label, "value": value},
                    ensure_ascii=False,
                )
            else:
                line = f"{timestamp}  {label} = {value!r}"
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

        def apply_values(report_keys: list, values: list[GrentonValue]) -> None:
            for key, value in zip(report_keys, values):
                label = label_for_key(key)
                if label in last_values and last_values[label] == value:
                    continue
                last_values[label] = value
                emit(label, value)

        api_client = GrentonCluCliApi(target_clu, config.encryption)

        _LOGGER.info("Connecting to CLU '%s'", target_clu.name)
        if not await api_client.connect():
            _LOGGER.error("✗ Failed to connect to CLU")
            sys.exit(1)

        try:
            await api_client.ping()

            async def handle_report(report_keys: list, report_values: list[GrentonValue]) -> None:
                apply_values(report_keys, report_values)

            api_client.on_subscription_report = handle_report

            _LOGGER.info("Watching %d %s(s) on '%s' (Ctrl+C to stop)", len(keys), kind, target_clu.name)
            values = await api_client.register_component_states(keys)
            if values is None:
                _LOGGER.error("✗ Failed to register %ss for subscription", kind)
                sys.exit(1)
            apply_values(keys, values)

            # The CLU subscription expires, so ping regularly and re-register periodically.
            elapsed = 0
            while True:
                await asyncio.sleep(_PING_INTERVAL)
                await api_client.ping()

                elapsed += _PING_INTERVAL
                if elapsed >= refresh_interval:
                    elapsed = 0
                    refreshed = await api_client.register_component_states(keys)
                    if refreshed is None:
                        _LOGGER.warning("Subscription refresh failed, retrying on next cycle")
                    else:
                        apply_values(keys, refreshed)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.error("✗ Error while watching %ss: %s", kind, e)
            sys.exit(1)
        finally:
            await api_client.disconnect()

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
            interface = json.loads(cache_path.read_text())
            discovered = self._discover_widgets(interface, clu_id)
            self._process_discovered_widgets(discovered, variable_labels, attribute_labels)
        except Exception as e:
            _LOGGER.error("Failed to parse interface cache: %s", e)

        return variable_labels, attribute_labels

    def _discover_widgets(self, interface: dict, clu_id: str) -> list[dict]:
        """Discover VARIABLE/ATTRIBUTE widgets from interface."""
        discovered = []

        for page in interface.get("pages", []):
            for widget in page.get("widgets", []):
                text = widget.get("text")
                if text and text.get("callType") in _STATE_TYPES and text.get("cluId") == clu_id:
                    discovered.append(self._extract_widget_data(widget, text))

                for comp in widget.get("components", []):
                    state = comp.get("state")
                    if state and state.get("callType") in _STATE_TYPES and state.get("cluId") == clu_id:
                        discovered.append(self._extract_component_data(comp, widget, state))

        return discovered

    @staticmethod
    def _extract_widget_data(widget: dict, text: dict) -> dict:
        """Extract data from widget's text state."""
        return {
            "label": widget.get("label") or text.get("label") or "",
            "objectName": text.get("objectName"),
            "index": text.get("index"),
            "callType": text.get("callType"),
        }

    @staticmethod
    def _extract_component_data(comp: dict, widget: dict, state: dict) -> dict:
        """Extract data from component's state."""
        return {
            "label": comp.get("label") or widget.get("label") or "",
            "objectName": state.get("objectName"),
            "index": state.get("index"),
            "callType": state.get("callType"),
        }

    def _process_discovered_widgets(
        self,
        discovered: list[dict],
        variable_labels: dict[str, list[str]],
        attribute_labels: dict[tuple[str, str], list[str]],
    ) -> None:
        """Process discovered widgets and populate label maps."""
        # Deduplicate
        seen = set()
        unique = []
        for d in discovered:
            key = (d["callType"], d["objectName"], d["index"], d["label"])
            if key not in seen:
                seen.add(key)
                unique.append(d)

        # Populate maps
        for entry in unique:
            label = (entry.get("label") or "").strip()
            if not label:
                continue

            if entry["callType"] == "VARIABLE" and entry["index"] is not None:
                variable_labels.setdefault(str(entry["index"]), []).append(label)
            elif entry["callType"] == "ATTRIBUTE" and entry["objectName"] and entry["index"] is not None:
                key = (str(entry["objectName"]), str(entry["index"]))
                attribute_labels.setdefault(key, []).append(label)

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
