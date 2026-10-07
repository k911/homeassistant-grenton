import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .coordinator import GrentonCoordinator
from .device_configuration import async_setup_device_configuration, configuration_url
from .domain.clu import GrentonClu
from .domain.encryption import GrentonEncryption
from .domain.entities.clu import GrentonCluEntity
from .domain.entities.clu_state import GrentonCluStateEntity
from .domain.entities.clu_state import clu_entities as create_clu_entities
from .domain.entities.clu_variables import (
    GrentonCluCustomVariable,
    GrentonCluVariableSensor,
    GrentonCluVariableSwitch,
)
from .domain.entities.on_off import GrentonEntityOnOff, configured_on_off_type
from .dto.mobile_interface import GrentonMobileInterfaceDto
from .frontend import async_register_scene_editor
from .integration_config import GrentonConfigEntry, GrentonConfigEntryData, RuntimeData
from .mappers.device_mapper import DeviceMapper
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.SENSOR, Platform.LIGHT, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.COVER, Platform.CAMERA]

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Make CLU actions available even before a config entry is loaded."""
    async_setup_services(hass)
    async_setup_device_configuration(hass)
    return True

async def async_setup_entry(hass: HomeAssistant, config_entry: GrentonConfigEntry) -> bool:
    _LOGGER.debug("Initializing Home Assistant Grenton integration")
    await async_register_scene_editor(hass)
    
    config_data: GrentonConfigEntryData = config_entry.data # type: ignore

    mobile_interface_dto = GrentonMobileInterfaceDto(**config_data["interface"])
    
    # Create coordinator first (before devices need it)
    encryption = GrentonEncryption.from_dto(mobile_interface_dto.encryption)
    clus = [GrentonClu.from_dto(clu) for clu in mobile_interface_dto.clus]
    coordinator = GrentonCoordinator(hass, config_entry, clus, encryption)
    
    _LOGGER.debug("Loaded interface with %d CLU(s)", len(clus))
    _LOGGER.debug("CLU details:")
    for clu in clus:
        _LOGGER.debug("- CLU %s (%s) at %s:%d", clu.name, clu.serial_number, clu.ip, clu.port)
    
    # Map mobile interface DTO to devices
    devices = DeviceMapper.from_mobile_interface(mobile_interface_dto, coordinator)
    for device in devices:
        for entity in device.entities:
            if info := entity.device_info:
                info["configuration_url"] = configuration_url(
                    config_entry.entry_id, device.id
                )

    _LOGGER.debug("Mapped %d device(s) from mobile interface", len(devices))
    _LOGGER.debug("Device details:")
    for device in devices:
        _LOGGER.debug("- Device %s (%s) with %d entity(ies)", device.type, device.id, len(device.entities))
        _LOGGER.debug("  Entities:")
        for entity in device.entities:
            _LOGGER.debug("  - Entity %s", entity.name)
    
    # Store runtime data
    clu_entities = [
        entity for clu in clus for entity in create_clu_entities(coordinator, clu)
    ]
    for entity in clu_entities:
        entity.device_info["configuration_url"] = configuration_url(
            config_entry.entry_id, f"clu_{entity.clu.id}"
        )
    config_entry.runtime_data = RuntimeData(
        coordinator=coordinator, devices=devices, clu_entities=clu_entities
    )

    # Drop entities and devices that no longer exist in the freshly fetched interface
    _cleanup_orphans(hass, config_entry, devices, clu_entities)

    # Setup the coordinator
    await coordinator.async_setup()

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    return True


def _cleanup_orphans(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    devices: list,
    clu_entities: list[GrentonCluEntity | GrentonCluStateEntity | GrentonCluCustomVariable],
) -> None:
    """Remove entity/device registry entries that no longer back a widget or CLU.

    Widget and CLU IDs provide stable registry identities. After a reconfigure
    that drops either, the old rows would otherwise linger as `unavailable`.
    """
    valid_entity_uids: set[str] = {
        entity.unique_id
        for device in devices
        for entity in device.entities
        if entity.unique_id
    }
    valid_device_identifiers: set[tuple[str, str]] = {
        ("grenton", device.id) for device in devices
    }
    valid_entity_uids.update(
        entity.unique_id for entity in clu_entities if entity.unique_id
    )
    for entity in clu_entities:
        if info := entity.device_info:
            valid_device_identifiers.update(info["identifiers"])

    configured_entity_domains = {
        entity.unique_id: configured_on_off_type(entity.coordinator, entity.unique_id)
        for device in devices
        for entity in device.entities
        if isinstance(entity, GrentonEntityOnOff)
    }

    configured_entity_domains.update(
        {
            entity.unique_id: "switch"
            if isinstance(entity, GrentonCluVariableSwitch)
            else "sensor"
            for entity in clu_entities
            if isinstance(entity, (GrentonCluVariableSwitch, GrentonCluVariableSensor))
        }
    )

    entity_reg = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(entity_reg, config_entry.entry_id):
        if entry.unique_id not in valid_entity_uids or (
            entry.unique_id in configured_entity_domains
            and entry.domain != configured_entity_domains[entry.unique_id]
        ):
            _LOGGER.debug("Removing orphaned entity %s (uid=%s)", entry.entity_id, entry.unique_id)
            entity_reg.async_remove(entry.entity_id)

    device_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_reg, config_entry.entry_id):
        if not any(identifier in valid_device_identifiers for identifier in device.identifiers):
            _LOGGER.debug("Removing orphaned device %s", device.id)
            if getattr(device, "config_entry_id", None) == config_entry.entry_id:
                # Current HA devices belong to one config entry.
                device_reg.async_remove_device(device.id)
            else:
                # Older HA versions can share a device across config entries.
                device_reg.async_update_device(device.id, remove_config_entry_id=config_entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    coordinator: GrentonCoordinator = config_entry.runtime_data.coordinator
    
    # Shutdown coordinator (close UDP sockets)
    await coordinator.async_shutdown()
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    
    return unload_ok
