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
        vol.Optional('tilt_started_ms', default=None): vol.Any(None, vol.All(int, vol.Range(min=1))),
    })

    def __call__(self, value: Any) -> str:
        if value not in ('finish', 'cancel', 'tilt_started'):
            raise vol.Invalid('Choose a measurement action')
        return value
