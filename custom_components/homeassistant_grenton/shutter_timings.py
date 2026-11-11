"""Coupled travel and slat timings for calibration review and adjustment."""

from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

from .domain.entities.roller_shutter_configuration import milliseconds


@selector.SELECTORS.register('grenton_shutter_timings')
class ShutterTimingsSelector(selector.Selector):
    selector_type = 'grenton_shutter_timings'
    CONFIG_SCHEMA = vol.Schema({
        vol.Optional('direction'): vol.In(('up', 'down')),
        vol.Optional('measured_ms', default=None): vol.Any(
            None, vol.All(int, vol.Range(min=1, max=1200000))),
        vol.Optional('measured_travel_ms', default=None): vol.Any(
            None, vol.All(int, vol.Range(min=1, max=600000))),
        vol.Optional('measured_lamel_ms', default=None): vol.Any(
            None, vol.All(int, vol.Range(min=1, max=600000))),
        vol.Optional('configured_up_ms', default=None): vol.Any(
            None, vol.All(int, vol.Range(min=0, max=600000))),
        vol.Optional('configured_down_ms', default=None): vol.Any(
            None, vol.All(int, vol.Range(min=0, max=600000))),
        vol.Optional('configured_lamel_ms', default=None): vol.Any(
            None, vol.All(int, vol.Range(min=0, max=600000))),
    })

    @staticmethod
    def validate(value: Any) -> dict[str, int | bool]:
        keys = {'total_ms', 'travel_ms', 'lamel_move_timeout'}
        if not isinstance(value, dict) or not keys <= value.keys() or value.keys() - keys - {'keep_total', 'adapt_opposite'}:
            raise vol.Invalid('Provide total, travel and slat times')
        keep_total = value.get('keep_total', True)
        if not isinstance(keep_total, bool):
            raise vol.Invalid('Choose whether to keep the total unchanged')
        adapt_opposite = value.get('adapt_opposite', False)
        if not isinstance(adapt_opposite, bool):
            raise vol.Invalid('Choose whether to adjust the opposite direction')
        values = {key: milliseconds(value[key], 1200000 if key == 'total_ms' else 600000)
                  for key in keys}
        if any(time <= 0 for time in values.values()):
            raise vol.Invalid('Movement times must be positive')
        if values['travel_ms'] + values['lamel_move_timeout'] != values['total_ms']:
            raise vol.Invalid('Travel and slat times must add up to the total')
        return {**values, 'keep_total': keep_total, 'adapt_opposite': adapt_opposite}

    def __call__(self, value: Any) -> dict[str, int | bool]:
        return self.validate(value)
