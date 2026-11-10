"""ROLLER_SHUTTER_V3 covers using the imported CLU bindings."""

import asyncio
from dataclasses import replace
from math import isfinite
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
import voluptuous as vol
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo

from ...coordinator import GrentonCoordinator
from ..action import GrentonAction, GrentonActionAttribute, GrentonActionMethod
from ..state_object import GrentonStateObject
from .base import BaseGrentonEntity
from .configurable import ConfigurableEntity
from .roller_shutter_configuration import SETTING_LIMITS, STATE_NAMES, ShutterConfigurationSchema, milliseconds
from .roller_shutter_calibration import ShutterCalibration


class GrentonEntityRollerShutterV3(BaseGrentonEntity, CoverEntity, ConfigurableEntity[ShutterConfigurationSchema]):
    """Expose native open percentage and optional blind slats as a HA cover."""

    _attr_device_class = CoverDeviceClass.SHUTTER

    def __init__(
        self,
        coordinator: GrentonCoordinator,
        id: str,
        label: str,
        cover_state: GrentonStateObject,
        cover_position: GrentonStateObject,
        set_position: GrentonAction,
        cover_tilt_position: GrentonStateObject | None = None,
        set_tilt_position: GrentonAction | None = None,
        stop: GrentonAction | None = None,
        device_info: DeviceInfo | None = None,
        *,
        lamel_move_timeout: GrentonStateObject | None = None,
        reverse_position: GrentonStateObject | None = None,
        setting_states: dict[str, GrentonStateObject] | None = None,
        setting_actions: dict[str, GrentonAction] | None = None,
        calibration_action: GrentonAction | None = None,
        up_state: GrentonStateObject | None = None,
        down_state: GrentonStateObject | None = None,
        legacy_max_time: GrentonStateObject | None = None,
    ) -> None:
        super().__init__(coordinator, id, label, device_info)
        self.cover_state = cover_state
        self.cover_position = cover_position
        self.cover_tilt_position = cover_tilt_position
        self.set_position = set_position
        self.set_tilt_position = set_tilt_position
        self.stop = stop
        self.lamel_move_timeout = lamel_move_timeout
        self.reverse_position = reverse_position
        self.setting_states = dict(setting_states or {})
        self.setting_actions = dict(setting_actions or {})
        if lamel_move_timeout is not None:
            self.setting_states.setdefault('lamel_move_timeout', lamel_move_timeout)
        if reverse_position is not None:
            self.setting_states.setdefault('reverse_position', reverse_position)
        self.calibration_action = calibration_action
        self.up_state, self.down_state = up_state, down_state
        self.legacy_max_time = legacy_max_time
        self._calibration: ShutterCalibration | None = None
        self._settings_lock = asyncio.Lock()
        for source in (cover_state, cover_position, cover_tilt_position, up_state, down_state):
            if source is not None:
                coordinator.register_component_state(source)
        for source in [*self.setting_states.values(), legacy_max_time]:
            if source is not None:
                coordinator.register_component_state(source, read_mode="slow")
        ConfigurableEntity.__init__(self)

    def _number(self, source: GrentonStateObject | None) -> float | None:
        if source is None:
            return None
        value = self.coordinator.get_value_for_component(source)
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if isfinite(number) else None

    @property
    def _tilt_enabled(self) -> bool:
        if self.cover_tilt_position is None or self.set_tilt_position is None:
            return False
        if isinstance(self._config.get('tilt_enabled'), bool):
            return self._config['tilt_enabled']
        if self.lamel_move_timeout is None:
            return True
        timeout = self._number(self.lamel_move_timeout)
        return timeout is not None and timeout > 0

    @property
    def _reversed(self) -> bool | None:
        if self.reverse_position is None:
            return False
        value = self._number(self.reverse_position)
        return bool(value) if value in (0, 1) else None

    @property
    def state_sources(self) -> list[GrentonStateObject]:
        sources = [self.cover_state, self.cover_position]
        if self._tilt_enabled and self.cover_tilt_position is not None:
            sources.append(self.cover_tilt_position)
        return sources

    @property
    def current_cover_position(self) -> int | None:
        value = self._number(self.cover_position)
        reverse = self._reversed
        if value is None or not 0 <= value <= 100 or reverse is None:
            return None
        return round(100 - value if reverse else value)

    @property
    def current_cover_tilt_position(self) -> int | None:
        if not self._tilt_enabled:
            return None
        value = self._number(self.cover_tilt_position)
        if value is None or not 0 <= value <= 90:
            return None
        return round(100 - value * 100 / 90)

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        return None if position is None else position == 0

    @property
    def is_closing(self) -> bool | None:
        state = self._number(self.cover_state)
        return None if state is None or state not in (0, 1, 2, 3, 4) else state == 2

    @property
    def is_opening(self) -> bool | None:
        state = self._number(self.cover_state)
        return None if state is None or state not in (0, 1, 2, 3, 4) else state == 1

    @staticmethod
    def _percentage(value: Any) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 100
            or not isfinite(value)
        ):
            raise ServiceValidationError("Cover position must be between 0 and 100")
        return float(value)

    async def _execute(self, template: GrentonAction, value: str | None = None) -> None:
        # Copy even fixed actions so concurrent calls cannot mutate the binding.
        action = replace(template)
        if value is not None:
            action.value = (
                template.value.replace("$value$", value)
                if "$value$" in template.value
                else value
            )
            if isinstance(action, GrentonActionMethod) and action.arguments is not None:
                action.arguments = tuple(
                    arg.replace("$value$", value) for arg in action.arguments
                )
        await self.coordinator.execute_action(action, raise_on_error=True)

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.async_set_cover_position(**{ATTR_POSITION: 100})

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.async_set_cover_position(**{ATTR_POSITION: 0})

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        if self._calibration is not None and self._calibration.running:
            raise ServiceValidationError('Stop calibration before moving the shutter')
        position = self._percentage(kwargs.get(ATTR_POSITION))
        reverse = self._reversed
        if reverse is None:
            raise ServiceValidationError(
                "The shutter position direction has not been received"
            )
        value = 100 - position if reverse else position
        await self._execute(self.set_position, f"{value:g}")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        if self._calibration is not None and self._calibration.running:
            await self._calibration.abort()
            return
        if self.stop is not None:
            await self._execute(self.stop)

    def _require_tilt(self) -> GrentonAction:
        if self._calibration is not None and self._calibration.running:
            raise ServiceValidationError('Stop calibration before moving the slats')
        if not self._tilt_enabled or self.set_tilt_position is None:
            raise ServiceValidationError("Slat control is not enabled for this shutter")
        return self.set_tilt_position

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        await self._execute(self._require_tilt(), "0")

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        await self._execute(self._require_tilt(), "90")

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        action = self._require_tilt()
        position = self._percentage(kwargs.get(ATTR_TILT_POSITION))
        await self._execute(action, str(round(90 - position * 90 / 100)))

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        self._require_tilt()
        await self.async_stop_cover()

    @property
    def supported_features(self) -> CoverEntityFeature:
        features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.SET_POSITION
        )
        if self.stop is not None:
            features |= CoverEntityFeature.STOP
        if self._tilt_enabled:
            features |= (
                CoverEntityFeature.OPEN_TILT
                | CoverEntityFeature.CLOSE_TILT
                | CoverEntityFeature.SET_TILT_POSITION
            )
            if self.stop is not None:
                features |= CoverEntityFeature.STOP_TILT
        return features

    def default_config(self):
        # Older configurations continue to derive the default from native timing.
        return {}

    @property
    def setting_values(self):
        values = {}
        for key, source in self.setting_states.items():
            value = self._number(source)
            if key == 'reverse_position':
                if value in (0, 1):
                    values[key] = bool(value)
            elif value is not None and value.is_integer() and 0 <= value <= SETTING_LIMITS[key]:
                values[key] = int(value)
        return values

    @property
    def calibration_available(self):
        return self.calibration_action is not None and self.stop is not None and all(
            key in self.setting_actions and key in self.setting_states
            for key in ('blinds_up_max_time', 'blinds_down_max_time')
        )

    def travel_time(self, direction):
        value = self.setting_values.get(f'blinds_{direction}_max_time', 0)
        legacy = self._number(self.legacy_max_time)
        return value or (int(legacy) if legacy is not None and 0 < legacy <= 600000 else 30000)

    def _get_schema_instance(self):
        return ShutterConfigurationSchema(
            self.setting_values, set(self.setting_actions) & set(self.setting_values),
            self.cover_tilt_position is not None and self.set_tilt_position is not None,
            self._tilt_enabled,
        )

    async def async_prepare_configuration(self):
        states = list(self.setting_states.values())
        if states and not await self.coordinator.async_refresh_clu_state(
            self.cover_state.clu_id, required_states=states
        ):
            raise HomeAssistantError('Could not read shutter configuration')
        if not (set(self.setting_actions) & set(self.setting_states)) <= self.setting_values.keys():
            raise HomeAssistantError('Shutter settings are unavailable or invalid')

    async def _write_setting(self, key, value):
        template = self.setting_actions.get(key)
        if template is None:
            raise ServiceValidationError('This shutter setting has no write binding')
        if isinstance(template, GrentonActionAttribute):
            await self.coordinator.execute_action(
                replace(template, value=str(int(value)), lua_value=str(int(value))),
                raise_on_error=True,
            )
        else:
            await self._execute(template, str(int(value)))

    async def apply_native_settings(self, settings, *, calibration=None):
        if settings:
            await self.async_prepare_configuration()
        validated = {}
        for key, value in settings.items():
            if key not in self.setting_actions or key not in self.setting_states:
                raise ServiceValidationError('This shutter setting is not supported')
            if key == 'reverse_position':
                if not isinstance(value, bool):
                    raise ServiceValidationError('Reverse position must be a boolean')
                validated[key] = value
            else:
                try:
                    validated[key] = milliseconds(value, SETTING_LIMITS[key])
                except (vol.Invalid, KeyError) as error:
                    raise ServiceValidationError(str(error)) from error
        async with self._settings_lock:
            active = self._calibration
            if calibration is not None and (
                active is not calibration or calibration.phase != 'review' or calibration.finished
            ):
                raise ServiceValidationError('This calibration result is no longer available')
            if active is not None and (active.running or (not active.finished and active is not calibration)):
                raise ServiceValidationError('Finish or cancel calibration before saving settings')
            for key, value in validated.items():
                if self.setting_values.get(key) != value:
                    await self._write_setting(key, value)
            states = [self.setting_states[key] for key in validated]
            if 'reverse_position' in validated:
                states += [self.cover_position, self.cover_state]
            if states:
                if not await self.coordinator.async_refresh_clu_state(
                    self.cover_state.clu_id, required_states=states
                ) or any(self.setting_values.get(key) != value for key, value in validated.items()):
                    raise HomeAssistantError('The CLU did not confirm the saved shutter settings')

    async def apply_configuration(self, user_input):
        enabled = user_input.get('tilt_enabled')
        if not isinstance(enabled, bool):
            raise ServiceValidationError('Choose whether slat control is supported')
        if enabled and (self.cover_tilt_position is None or self.set_tilt_position is None):
            raise ServiceValidationError('Slat bindings are unavailable')
        settings = {key: value for key, value in user_input.items() if key != 'tilt_enabled'}
        if enabled and 'lamel_move_timeout' in self.setting_actions:
            timeout = settings.get('lamel_move_timeout', self.setting_values.get('lamel_move_timeout', 0))
            try:
                if milliseconds(timeout) <= 0:
                    raise vol.Invalid('Enter a positive slat movement time')
            except vol.Invalid as error:
                raise ServiceValidationError(str(error)) from error
        await self.apply_native_settings(settings)
        options = await ConfigurableEntity.apply_configuration(self, {'tilt_enabled': enabled})
        if self.hass is not None:
            self.async_write_ha_state()
        return options

    async def create_calibration(self, direction, limit_ms, lamel_ms=0):
        async with self._settings_lock:
            if not self.calibration_available:
                raise HomeAssistantError('Calibration bindings are unavailable')
            if self._calibration is not None and (self._calibration.running or self._calibration._temporary or not self._calibration.finished):
                raise HomeAssistantError('A calibration is already active')
            if self._number(self.cover_state) in (3, 4):
                raise HomeAssistantError('The shutter is blocked or already calibrating')
            self._calibration = ShutterCalibration(self, direction, limit_ms, lamel_ms)
            return self._calibration

    async def async_will_remove_from_hass(self):
        try:
            if self._calibration is not None:
                await self._calibration.abort()
        finally:
            await super().async_will_remove_from_hass()

    @callback
    def async_entity_id_changed(self, old_entity_id):
        # All subscriptions and calibration ownership use CLU/widget IDs.
        super().async_entity_id_changed(old_entity_id)

    @property
    def extra_state_attributes(self):
        attributes = dict(super().extra_state_attributes or {})
        code = self._number(self.cover_state)
        attributes['grenton_state'] = STATE_NAMES.get(code, 'unknown')
        for key, source in (('up_relay_active', self.up_state), ('down_relay_active', self.down_state)):
            value = self._number(source)
            if source is not None:
                attributes[key] = bool(value) if value in (0, 1) else None
        return attributes
