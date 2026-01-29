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

from config import load_configuration, save_configuration

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
