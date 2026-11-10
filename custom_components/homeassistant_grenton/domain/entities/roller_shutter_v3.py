"""ROLLER_SHUTTER_V3 covers using the imported CLU bindings."""

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
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo

from ...coordinator import GrentonCoordinator
from ..action import GrentonAction, GrentonActionMethod
from ..state_object import GrentonStateObject
from .base import BaseGrentonEntity


class GrentonEntityRollerShutterV3(BaseGrentonEntity, CoverEntity):
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
        for source in (cover_state, cover_position, cover_tilt_position):
            if source is not None:
                coordinator.register_component_state(source)
        for source in (lamel_move_timeout, reverse_position):
            if source is not None:
                coordinator.register_component_state(source, read_mode="slow")

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
        position = self._percentage(kwargs.get(ATTR_POSITION))
        reverse = self._reversed
        if reverse is None:
            raise ServiceValidationError(
                "The shutter position direction has not been received"
            )
        value = 100 - position if reverse else position
        await self._execute(self.set_position, f"{value:g}")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        if self.stop is not None:
            await self._execute(self.stop)

    def _require_tilt(self) -> GrentonAction:
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
