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
            _LOGGER.info(f"Connecting to Grenton Object Manager at {url}")
            
            async with GrentonObjectManagerApi(url) as api:
                interface_data = await api.fetch_mobile_interface(pin)
            
            _LOGGER.info("✓ Successfully fetched mobile interface")
            _LOGGER.info(f"✓ Interface ID: {interface_data.get('id')}")
            _LOGGER.info(f"✓ Interface name: {interface_data.get('name')}")
            _LOGGER.info(f"✓ Version: {interface_data.get('version')}")
            
            encryption, clus = self._create_objects(interface_data)
            save_configuration(interface_data, config_path)
            save_interface_cache(interface_data)
            
            await self._test_clus(clus, encryption)
        
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
    
    async def test_connection(self, config_path) -> None:
        """Test connection using saved configuration."""
        result = load_configuration(config_path)
        if not result:
            sys.exit(1)
        
        encryption, clus = result
        await self._test_clus(clus, encryption)
    
    async def view_clu_state(self, config_path, clu_name: str) -> None:
        """View CLU state with all attributes and variables using coordinator pattern."""
        from homeassistant_grenton.state import GrentonState, GrentonCluState, GrentonCluStateVariableKey, GrentonCluStateAttributeKey
        
        result = load_configuration(config_path)
        if not result:
            sys.exit(1)
        
        encryption, clus = result
        
        # Find CLU by name
        target_clu = next((c for c in clus if c.name == clu_name), None)
        if not target_clu:
            available = ", ".join([c.name for c in clus])
            _LOGGER.error(f"✗ CLU '{clu_name}' not found. Available CLUs: {available}")
            sys.exit(1)
        
        try:
            _LOGGER.info(f"Connecting to CLU '{target_clu.name}'")
            
            api_client = GrentonCluApi(target_clu, encryption)
            if not await api_client.connect():
                _LOGGER.error("✗ Failed to connect to CLU")
                sys.exit(1)
            
            _LOGGER.info("✓ Connected successfully")
            
                # Initialize CLU state (using coordinator pattern)
            clu_state = GrentonCluState()

            # Parse the cached interface.json and register discovered states
            cache_path = Path.home() / ".grenton" / "cache" / "interface.json"
            variable_labels, attribute_labels = self._parse_interface_cache_for_clu(cache_path, target_clu.id)

            # Register discovered states into clu_state
            self._register_discovered_states(clu_state, variable_labels, attribute_labels)

            # Ping to keep alive
            await api_client.ping()

            # Register component states to get initial values
            _LOGGER.info("Fetching CLU state...")

            result_vars: list[str] = []
            result_attrs: list[str] = []

            if clu_state.has_states_to_register():
                keys = clu_state.get_subscription_order()
                values = await api_client.register_component_states(keys)

                # Update state with returned values and collect grouped output
                if values:
                    for key, value in zip(keys, values):
                        if isinstance(key, GrentonCluStateVariableKey):
                            clu_state.set_variable(key, value)

                            label = variable_labels.get(key.name, key.name)
                            object_name = target_clu.id
                            result_vars.append(f"{label} - {object_name} [{key.name}] - {value}")

                        elif isinstance(key, GrentonCluStateAttributeKey):
                            clu_state.set_attribute(key, value)

                            label = attribute_labels.get((key.object_name, key.name), f"{key.object_name}.{key.name}")
                            result_attrs.append(f"{label} - {key.object_name} [{key.name}] - {value}")

            else:
                _LOGGER.debug("[%s] No component states to register", target_clu.id)

            await api_client.disconnect()

            # Display grouped results
            if result_vars or result_attrs:
                print("\n" + "=" * 80)
                print(f"CLU STATE: {target_clu.name} (ID: {target_clu.id})")
                print("=" * 80)
                if result_vars:
                    print("\nVARIABLES:")
                    print("-" * 80)
                    for line in result_vars:
                        print(line)
                if result_attrs:
                    print("\nATTRIBUTES:")
                    print("-" * 80)
                    for line in result_attrs:
                        print(line)
                print("=" * 80 + "\n")
            else:
                print("\n  No VARIABLE/ATTRIBUTE widgets registered for this CLU in the interface cache\n")
            _LOGGER.info("✓ State view completed successfully")
            sys.exit(0)
        
        except Exception as e:
            _LOGGER.error(f"✗ Error viewing CLU state: {e}")
            sys.exit(1)

    def _parse_interface_cache_for_clu(self, cache_path: Path, clu_id: str) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
        """Parse the cached interface JSON and return variable and attribute label maps for a CLU."""
        variable_labels: dict[str, str] = {}
        attribute_labels: dict[tuple[str, str], str] = {}

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

            # Deduplicate
            seen = set()
            unique = []
            for d in discovered:
                key = (d.get("callType"), d.get("objectName"), d.get("index"))
                if key not in seen:
                    seen.add(key)
                    unique.append(d)

            for entry in unique:
                ctype = entry.get("callType")
                label = entry.get("label") or ""
                obj = entry.get("objectName") or entry.get("cluId")
                idx = entry.get("index")

                if ctype == "VARIABLE":
                    variable_labels[idx] = label
                elif ctype == "ATTRIBUTE":
                    attribute_labels[(obj, idx)] = label

        except Exception as e:
            _LOGGER.error("Failed to parse interface cache: %s", e)

        return variable_labels, attribute_labels

    def _register_discovered_states(self, clu_state: GrentonCluState, variable_labels: dict[str, str], attribute_labels: dict[tuple[str, str], str]) -> None:
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
                        _LOGGER.info(f"✓ CLU '{clu.name}' connected and pinged successfully")
                    else:
                        _LOGGER.error(f"✗ CLU '{clu.name}' ping failed")
                        all_connected = False
                    await api_client.disconnect()
                else:
                    _LOGGER.error(f"✗ CLU '{clu.name}' connection failed")
                    all_connected = False
            except Exception as e:
                _LOGGER.error(f"✗ CLU '{clu.name}' connection error: {e}")
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
        _LOGGER.info(f"✓ Loaded {len(clus)} CLU(s)")
        
        return encryption, clus
