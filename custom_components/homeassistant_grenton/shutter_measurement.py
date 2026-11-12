"""Frontend stopwatch display; the backend remains the measurement clock."""

from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector


@selector.SELECTORS.register('grenton_shutter_measurement')
class ShutterMeasurementSelector(selector.Selector):
    selector_type = 'grenton_shutter_measurement'
    CONFIG_SCHEMA = vol.Schema({
        vol.Required('flow_id'): str,
        vol.Required('direction'): vol.In(('up', 'down')),
        vol.Required('elapsed_ms'): vol.All(int, vol.Range(min=0)),
        vol.Optional('segment', default=None): vol.Any(None, vol.In(('tilt', 'travel'))),
        vol.Optional('segment_started_ms', default=0): vol.All(int, vol.Range(min=0)),
    })

    def __call__(self, value: Any) -> str:
        if value not in ('finish', 'cancel', 'tilt_started', 'tilt_finished'):
            raise vol.Invalid('Choose a measurement action')
        return value
