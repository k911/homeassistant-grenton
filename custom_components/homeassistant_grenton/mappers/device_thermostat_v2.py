"""Map a thermostat widget to its heating climate entity."""

from ..coordinator import GrentonCoordinator
from ..domain.action import GrentonAction
from ..domain.devices.thermostat_v2 import GrentonDeviceThermostatV2
from ..domain.entities.thermostat_v2 import GrentonEntityThermostatV2
from ..domain.state_object import GrentonStateObject
from ..dto.widgets.thermostat_v2 import GrentonWidgetThermostatV2Dto


class DeviceThermostatV2Mapper:
    @staticmethod
    def to_domain(
        dto: GrentonWidgetThermostatV2Dto, coordinator: GrentonCoordinator
    ) -> GrentonDeviceThermostatV2:
        device = GrentonDeviceThermostatV2(type=dto.type, id=dto.id, entities=[])
        bindings = dto.object
        states = {
            name: GrentonStateObject.from_dto(getattr(bindings, name))
            for name in (
                "state",
                "mode",
                "currentTemperature",
                "targetTemperature",
                "minTemperature",
                "maxTemperature",
                "controlOutValue",
                "scheduleData",
            )
        }
        actions = {
            name: GrentonAction.from_dto(getattr(bindings, name))
            for name in (
                "setStateAction",
                "setModeAction",
                "setTargetTemperatureAction",
                "setScheduleDataAction",
            )
        }
        device.entities = [
            GrentonEntityThermostatV2(
                coordinator=coordinator,
                id=f"{dto.id}_climate",
                label=dto.label,
                states=states,
                actions=actions,
                device_info=device.device_info,
            )
        ]
        return device
