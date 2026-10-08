"""Select sensor or binary sensor presentation for VALUE_V2 and VALUE_DOUBLE."""

from dataclasses import dataclass
from math import isfinite

import voluptuous as vol
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import CONF_UNIT_OF_MEASUREMENT
from homeassistant.helpers import selector

from .binary_sensor import GrentonEntityBinarySensor
from .configurable import StepDefinition, StepResult
from .value import GrentonEntityValue, GrentonEntityValueConfigurationSchema


def configured_value_v2_type(coordinator, unique_id: str) -> str:
    """Resolve the domain before forwarding entities to HA platforms."""
    config = coordinator.config_entry.options.get("entities", {}).get(unique_id, {})
    return "binary_sensor" if config.get("entity_type") == "binary_sensor" else "sensor"


@dataclass
class ValueV2ConfigurationSchema(GrentonEntityValueConfigurationSchema):
    """Offer only classes and units belonging to the selected entity type."""

    @property
    def steps(self):
        return [
            StepDefinition("configure_value_v2_type", self._build_type),
            StepDefinition("configure_sensor_class", self._build_step_class_schema),
            StepDefinition("configure_sensor_unit", self._build_step_unit_schema),
        ]

    def _build_type(self, current, accumulated):
        return StepResult(
            schema=vol.Schema(
                {
                    vol.Required(
                        "entity_type", default=current.get("entity_type", "sensor")
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["sensor", "binary_sensor"],
                            translation_key="value_v2_entity_types",
                        )
                    )
                }
            )
        )

    def _build_step_class_schema(self, current, accumulated):
        binary = accumulated.get("entity_type") == "binary_sensor"
        classes = BinarySensorDeviceClass if binary else SensorDeviceClass
        options = ["none", *[device_class.value for device_class in classes]]
        default = current.get("device_class", "none")
        if default not in options:
            default = "none"
        return StepResult(
            step_id="configure_binary_sensor_class"
            if binary
            else "configure_sensor_class",
            schema=vol.Schema(
                {
                    vol.Required(
                        "device_class", default=default
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            translation_key="binary_sensor_device_classes"
                            if binary
                            else "sensor_device_classes",
                            sort=True,
                        )
                    )
                }
            ),
        )

    def _build_step_unit_schema(self, current, accumulated):
        if accumulated.get("entity_type") == "binary_sensor":
            return StepResult(schema=vol.Schema({}), complete=True)
        return super()._build_step_unit_schema(current, accumulated)


class ValueV2Configuration:
    """Share configuration across domains while keeping the widget identity."""

    reload_after_configuration = True

    def _get_schema_instance(self):
        return ValueV2ConfigurationSchema()

    async def apply_configuration(self, user_input):
        config = dict(user_input)
        if config.get("device_class") in (None, "none"):
            config.pop("device_class", None)
            config.pop(CONF_UNIT_OF_MEASUREMENT, None)
        if (
            config.get("entity_type") == "binary_sensor"
            or config.get("device_class") == SensorDeviceClass.ENUM
            or config.get(CONF_UNIT_OF_MEASUREMENT) == "none"
        ):
            config.pop(CONF_UNIT_OF_MEASUREMENT, None)
        return await super().apply_configuration(config)


class GrentonValueV2Sensor(ValueV2Configuration, GrentonEntityValue):
    """Default numeric or textual value sensor."""


class GrentonValueV2BinarySensor(ValueV2Configuration, GrentonEntityBinarySensor):
    """Convert finite numeric values to a binary state: positive means on."""

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.get_value_for_component(self.state_object)
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not isfinite(numeric):
            return None
        return numeric > 0


def value_v2_entity(*, coordinator, id, label, state_object, value_type, device_info):
    """Map one value consistently for single and double value widgets."""
    if configured_value_v2_type(coordinator, id) == "binary_sensor":
        return GrentonValueV2BinarySensor(
            coordinator=coordinator,
            id=id,
            label=label,
            state_object=state_object,
            device_info=device_info,
            reversed=False,
        )
    return GrentonValueV2Sensor(
        coordinator=coordinator,
        id=id,
        label=label,
        state_object=state_object,
        device_info=device_info,
        value_type=value_type,
    )
