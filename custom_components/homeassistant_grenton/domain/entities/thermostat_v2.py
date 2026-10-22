"""Heating, optional cooling and schedules for Grenton THERMOSTAT_V2 widgets."""

import asyncio
import math
from dataclasses import replace
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import ATTR_HVAC_MODE
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo

from ...coordinator import GrentonCoordinator
from ..action import GrentonAction
from ..state_object import GrentonStateObject
from ..thermostat_schedule import ThermostatSchedule, decode_schedule, encode_schedule
from .base import BaseGrentonEntity
from .configurable import ConfigurableEntity
from .thermostat_configuration import (
    GrentonThermostatConfigurationSchema,
    schedule_sections,
)

MODE_VALUES = {HVACMode.HEAT: 0, HVACMode.COOL: 0, HVACMode.AUTO: 2}


class GrentonEntityThermostatV2(
    BaseGrentonEntity,
    ClimateEntity,
    ConfigurableEntity[GrentonThermostatConfigurationSchema],
):
    """A thermostat with manual heating/cooling and CLU schedule operation."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_supported_features = (
        ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
    )
    # BaseGrentonEntity always assigns the widget's non-null ID.
    unique_id: str

    def __init__(
        self,
        coordinator: GrentonCoordinator,
        id: str,
        label: str,
        states: dict[str, GrentonStateObject],
        actions: dict[str, GrentonAction],
        device_info: DeviceInfo | None = None,
        cooling_detected: bool = False,
    ) -> None:
        super().__init__(coordinator, id, label, device_info)
        self.states = states
        self.actions = actions
        self._cooling_detected = cooling_detected
        ConfigurableEntity.__init__(self)
        self._command_lock = asyncio.Lock()
        for state in states.values():
            coordinator.register_component_state(state)

    @property
    def name(self) -> None:
        # This remains the main entity when the read-only schedule is added.
        return None

    @property
    def decoded_schedule(self) -> ThermostatSchedule | None:
        data, maximum = self._value("scheduleData"), self._number("maxTemperature")
        if isinstance(data, str) and maximum is not None:
            try:
                return decode_schedule(data, maximum)
            except ValueError:
                pass
        return None

    @property
    def following_schedule(self) -> bool | None:
        state, mode = self._number("state"), self._number("mode")
        if state == 0 or mode in (0, 1, 3):
            return False
        if state == 1 and mode == 2:
            return True
        return None

    @property
    def cooling_enabled(self) -> bool:
        return self._config.get("cooling_enabled", self._cooling_detected) is True

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return [
            HVACMode.OFF,
            HVACMode.HEAT,
            *([HVACMode.COOL] if self.cooling_enabled else []),
            HVACMode.AUTO,
        ]

    def default_config(self) -> dict[str, Any]:
        return {"cooling_enabled": self._cooling_detected}

    def _get_schema_instance(self) -> GrentonThermostatConfigurationSchema:
        minimum, maximum = (
            self._number("minTemperature"),
            self._number("maxTemperature"),
        )
        settings: dict[str, Any] = {
            "cooling_available": "controlDirection" in self.states
            and "setControlDirectionAction" in self.actions,
            "schedule_available": False,
        }
        sections: list[dict[str, Any]] = []
        direction = self.control_direction_name
        if direction is not None:
            settings["direction"] = direction
        data = self._value("scheduleData")
        if minimum is not None and maximum is not None and isinstance(data, str):
            try:
                sections = schedule_sections(decode_schedule(data, maximum).periods())
            except ValueError:
                pass
            else:
                settings.update(
                    schedule_available=minimum < maximum,
                    minimum=minimum,
                    maximum=maximum,
                )
        return GrentonThermostatConfigurationSchema(settings, sections)

    async def apply_configuration(self, user_input: dict[str, Any]) -> dict[str, Any]:
        # Keep the CLU authoritative: do not persist a second copy of its schedule.
        direction = user_input.get("control_direction")
        if direction is not None and (
            direction not in ("heating", "cooling") or not user_input["cooling_enabled"]
        ):
            raise ServiceValidationError(
                "Enable cooling before selecting its direction"
            )
        async with self._command_lock:
            if "schedule" in user_input:
                await self._set_schedule(user_input["schedule"])
            if direction is not None:
                await self._execute(
                    "setControlDirectionAction", "1" if direction == "cooling" else "0"
                )
        options = await ConfigurableEntity.apply_configuration(
            self, {"cooling_enabled": user_input["cooling_enabled"]}
        )
        if self.hass is not None:
            self.async_write_ha_state()
        return options

    def _value(self, name: str) -> Any:
        state = self.states.get(name)
        return (
            self.coordinator.get_value_for_component(state)
            if state is not None
            else None
        )

    def _direction(self) -> float | None:
        direction = self._number("controlDirection")
        # Preserve heating-only interfaces before direction feedback arrives.
        return 0 if direction is None and not self.cooling_enabled else direction

    @property
    def control_direction_name(self) -> str | None:
        direction = self._number("controlDirection")
        return "heating" if direction == 0 else "cooling" if direction == 1 else None

    def _number(self, name: str) -> float | None:
        value = self._value(name)
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @property
    def current_temperature(self) -> float | None:
        return self._number("currentTemperature")

    @property
    def target_temperature(self) -> float | None:
        # TargetTemp is the effective target in either manual or schedule mode.
        # PointValue (the write binding) is only the manual target.
        return self._number("targetTemperature")

    @property
    def supported_features(self) -> ClimateEntityFeature:
        # HA's thermostat controls are editable whenever TARGET_TEMPERATURE is
        # advertised, even in AUTO. Advertise it only for the manual setpoint.
        features = self._attr_supported_features
        if self._number("mode") == 0:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        return features

    @property
    def min_temp(self) -> float:
        value = self._number("minTemperature")
        return value if value is not None else super().min_temp

    @property
    def max_temp(self) -> float:
        value = self._number("maxTemperature")
        return value if value is not None else super().max_temp

    @property
    def hvac_mode(self) -> HVACMode | None:
        value = self._number("state")
        if value == 0:
            return HVACMode.OFF
        if value == 1:
            mode = self._number("mode")
            if mode == 2:
                return HVACMode.AUTO
            if mode == 0:
                if self._direction() == 0:
                    return HVACMode.HEAT
                if self._direction() == 1 and self.cooling_enabled:
                    return HVACMode.COOL
        return None

    @property
    def hvac_action(self) -> HVACAction | None:
        state = self._number("state")
        if state == 0:
            return HVACAction.OFF
        if state != 1:
            return None
        output = self._number("controlOutValue")
        if output == 1:
            direction = self._direction()
            if direction == 0:
                return HVACAction.HEATING
            if direction == 1:
                return HVACAction.COOLING
        if output == 0:
            return HVACAction.IDLE
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Preserve effective scheduled targets when editing is not advertised.
        attributes = {
            "grenton_mode": self._value("mode"),
            "control_direction": self._value("controlDirection"),
            "control_direction_name": self.control_direction_name,
        }
        if self._number("mode") == 2:
            attributes["scheduled_target_temperature_celsius"] = self.target_temperature
        if schedule := self.decoded_schedule:
            attributes["weekly_schedule"] = schedule.periods()
        return attributes

    async def _execute(self, action_name: str, value: str) -> None:
        # Never change the imported template, including during concurrent calls.
        if action_name not in self.actions:
            raise ServiceValidationError("The thermostat action is not configured")
        await self.coordinator.execute_action(
            replace(self.actions[action_name], value=value), raise_on_error=True
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(f"Unsupported HVAC mode: {hvac_mode}")
        async with self._command_lock:
            await self._set_hvac_mode(hvac_mode)

    async def _set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Change mode and power while the caller holds the command lock."""
        if hvac_mode != HVACMode.OFF:
            if hvac_mode in (HVACMode.HEAT, HVACMode.COOL) and (
                "setControlDirectionAction" in self.actions
            ):
                await self._execute(
                    "setControlDirectionAction",
                    "1" if hvac_mode == HVACMode.COOL else "0",
                )
            await self._execute("setModeAction", str(MODE_VALUES[hvac_mode]))
        await self._execute("setStateAction", "0" if hvac_mode == HVACMode.OFF else "1")

    async def async_turn_on(self) -> None:
        # Start in the existing mode; selecting Heat explicitly selects manual.
        async with self._command_lock:
            await self._execute("setStateAction", "1")

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the manual target; schedule operation must be left explicitly."""
        requested_mode = kwargs.get(ATTR_HVAC_MODE)
        if requested_mode is not None and requested_mode not in self.hvac_modes:
            raise ServiceValidationError(f"Unsupported HVAC mode: {requested_mode}")
        value = kwargs.get(ATTR_TEMPERATURE)
        if value is None:
            if requested_mode is not None:
                await self.async_set_hvac_mode(requested_mode)
            return
        try:
            temperature = float(value)
        except (ValueError, TypeError) as error:
            raise ServiceValidationError(
                "Target temperature must be a number"
            ) from error
        if (
            isinstance(value, bool)
            or not math.isfinite(temperature)
            or not self.min_temp <= temperature <= self.max_temp
        ):
            raise ServiceValidationError(
                f"Target temperature must be between {self.min_temp} and {self.max_temp} °C"
            )
        if not math.isclose(temperature * 2, round(temperature * 2), abs_tol=1e-6):
            raise ServiceValidationError(
                "Target temperature must use 0.5 °C increments"
            )
        temperature = round(temperature * 2) / 2
        async with self._command_lock:
            if requested_mode == HVACMode.AUTO or self._number("mode") != 0:
                raise ServiceValidationError(
                    "Select Heat or Cool (manual mode) before changing the target temperature"
                )
            # Updating a target never leaves schedule mode implicitly.
            await self._execute("setTargetTemperatureAction", str(temperature))
            if requested_mode is not None:
                await self._set_hvac_mode(requested_mode)

    async def async_set_thermostat_schedule(self, schedule: Any) -> None:
        """Replace the complete week without changing mode, power or metadata."""
        async with self._command_lock:
            await self._set_schedule(schedule)

    async def _set_schedule(self, schedule: Any) -> None:
        """Write the week while the caller holds the command lock."""
        data = self._value("scheduleData")
        if not isinstance(data, str) or not data:
            raise ServiceValidationError(
                "The thermostat schedule has not been received yet"
            )
        minimum = self._number("minTemperature")
        maximum = self._number("maxTemperature")
        if minimum is None or maximum is None:
            raise ServiceValidationError(
                "The thermostat temperature limits have not been received yet"
            )
        try:
            payload = encode_schedule(schedule, data, minimum, maximum)
        except (TypeError, ValueError) as error:
            raise ServiceValidationError(str(error)) from error
        await self._execute("setScheduleDataAction", payload)
