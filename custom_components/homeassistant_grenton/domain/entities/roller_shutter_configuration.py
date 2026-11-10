"""Native shutter settings; only tilt capability is stored in HA options."""

from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

from .configurable import BaseGrentonEntityConfigurationSchema, StepDefinition, StepResult

SETTING_LIMITS = {
    'lamel_move_timeout': 600000,
    'mechanical_offset': 1000,
    'blinds_up_max_time': 600000,
    'blinds_down_max_time': 600000,
}
STATE_NAMES = {0: 'stopped', 1: 'opening', 2: 'closing', 3: 'blocked', 4: 'calibrating'}


def milliseconds(value: Any, maximum: int = 600000) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise vol.Invalid('Enter a whole number of milliseconds')
    if not 0 <= value <= maximum or int(value) != value:
        raise vol.Invalid(f'Enter a whole number between 0 and {maximum} ms')
    return int(value)


def number_selector(maximum: int = 600000):
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=0, max=maximum, step=1, unit_of_measurement='ms',
        mode=selector.NumberSelectorMode.BOX,
    ))


@dataclass
class ShutterConfigurationSchema(BaseGrentonEntityConfigurationSchema):
    values: dict[str, Any]
    writable: set[str]
    tilt_available: bool
    tilt_enabled: bool

    @property
    def steps(self):
        return [StepDefinition('configure_shutter_tilt', self._tilt),
                StepDefinition('configure_shutter_settings', self._settings)]

    def _tilt(self, current, accumulated):
        def validate(data):
            if data['tilt_enabled'] and not self.tilt_available:
                raise vol.Invalid('Slat bindings are not configured', path=['tilt_enabled'])
            return data
        return StepResult(schema=vol.Schema({
            vol.Required('tilt_enabled', default=current.get('tilt_enabled', self.tilt_enabled)):
                selector.BooleanSelector(),
        }), validator=validate)

    def _settings(self, current, accumulated):
        fields = {}
        for key, maximum in SETTING_LIMITS.items():
            if key not in self.writable or (key == 'lamel_move_timeout' and not accumulated.get('tilt_enabled')):
                continue
            fields[vol.Required(key, default=current.get(key, self.values.get(key, 0)))] = number_selector(maximum)
        if 'reverse_position' in self.writable:
            fields[vol.Required('reverse_position', default=current.get('reverse_position', self.values.get('reverse_position', False)))] = selector.BooleanSelector()

        def validate(data):
            for key, value in data.items():
                if key in SETTING_LIMITS:
                    try:
                        data[key] = milliseconds(value, SETTING_LIMITS[key])
                    except vol.Invalid as error:
                        raise vol.Invalid(str(error), path=[key]) from error
            if accumulated.get('tilt_enabled') and 'lamel_move_timeout' in data and data['lamel_move_timeout'] == 0:
                raise vol.Invalid('Enter a positive slat movement time', path=['lamel_move_timeout'])
            return data
        return StepResult(schema=vol.Schema(fields), validator=validate)
