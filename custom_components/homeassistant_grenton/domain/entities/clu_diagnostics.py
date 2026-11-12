"""CLU connection diagnostics and optional diagnostic collection switches."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from ...coordinator import GrentonCoordinator
from ..clu import GrentonClu
from ..device_types import attribute_index
from .clu import clu_device_info


class CluConnectionDiagnostic(Entity):
    """Remain available during outages so the device page can explain them."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self, coordinator: GrentonCoordinator, clu: GrentonClu, key: str
    ) -> None:
        self.coordinator = coordinator
        self.clu = clu
        self._attr_unique_id = f"clu_{clu.id}_{key.removeprefix('clu_')}"
        self._attr_translation_key = key
        self._attr_device_info = clu_device_info(clu)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self.coordinator.health_update_signal(self.clu.id),
                self._update,
            )
        )

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Keep the health listener, which is keyed by CLU ID, on rename."""
        super().async_entity_id_changed(old_entity_id)

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class CluConnection(CluConnectionDiagnostic, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.connection_health[self.clu.id].connected


class CluRequestTimeouts(CluConnectionDiagnostic, SensorEntity):
    _attr_icon = "mdi:timer-alert-outline"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> int:
        return self.coordinator.connection_health[self.clu.id].request_timeouts


class CluSubscriptions(CluConnectionDiagnostic, SensorEntity):
    _attr_icon = "mdi:lan-connect"

    @property
    def native_value(self) -> int:
        return self.coordinator.subscription_count(self.clu.id)


class CluLastRestart(CluConnectionDiagnostic, SensorEntity):
    """Estimated boot timestamp derived from confirmed uptime observations."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:restart"

    @property
    def native_value(self):
        return self.coordinator.restarts.records[self.clu.id].boot_time

    @property
    def extra_state_attributes(self):
        record = self.coordinator.restarts.records[self.clu.id]
        return {
            "detected_at": record.detected_at.isoformat() if record.detected_at else None,
            "previous_uptime": record.previous_uptime,
            "uptime_at_detection": record.uptime,
            "debug_capture_status": record.capture_status,
            "debug_capture_error": record.capture_error,
            "debug_capture_file": record.capture_file,
        }


class CluDebugCapture(CluConnectionDiagnostic, SwitchEntity):
    """Local automatic dump preference; never changes native verbosity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bug-check-outline"

    @property
    def is_on(self) -> bool:
        return self.coordinator.restarts.capture_enabled(self.clu.id)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.restarts.async_set_capture(self.clu.id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.restarts.async_set_capture(self.clu.id, False)


class CluTelnetLoggingSwitch(CluConnectionDiagnostic, SwitchEntity):
    """Own a Telnet stream independently from native verbosity and file capture."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:console-network-outline"

    @property
    def is_on(self) -> bool:
        return self.coordinator.telnet_logs.is_on(self.clu.id)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.telnet_logs.async_start(self.clu.id)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.telnet_logs.async_stop(self.clu.id)


def connection_diagnostics(
    coordinator: GrentonCoordinator, clu: GrentonClu
) -> list[Entity]:
    entities = [
        entity(coordinator, clu, key)
        for entity, key in (
            (CluConnection, "clu_connection"),
            (CluRequestTimeouts, "clu_request_timeouts"),
            (CluSubscriptions, "clu_subscriptions"),
        )
    ]
    if attribute_index(clu.device_type, "Uptime") is not None:
        entities.extend([
            CluLastRestart(coordinator, clu, "clu_last_restart"),
            CluDebugCapture(coordinator, clu, "clu_debug_capture"),
        ])
    if attribute_index(clu.device_type, "TelnetLogLevel") is not None:
        entities.append(CluTelnetLoggingSwitch(coordinator, clu, "clu_telnet_logging"))
    return entities
