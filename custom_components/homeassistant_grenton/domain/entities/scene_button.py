from dataclasses import dataclass, replace
from typing import Any

import voluptuous as vol
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import DeviceInfo

from ...coordinator import GrentonCoordinator
from ..action import (
    GrentonAction,
    GrentonActionAttribute,
    GrentonActionMethod,
    GrentonActionScript,
    GrentonActionVariable,
)
from ..api.clu_messages.action import GrentonCluApiActionRequest
from ..enums import GrentonActionCallType
from .base import BaseGrentonEntity
from .configurable import (
    BaseGrentonEntityConfigurationSchema,
    ConfigurableEntity,
    StepDefinition,
    StepResult,
)

ACTION_CLASSES = {
    GrentonActionCallType.ATTRIBUTE: GrentonActionAttribute,
    GrentonActionCallType.METHOD: GrentonActionMethod,
    GrentonActionCallType.SCRIPT: GrentonActionScript,
    GrentonActionCallType.VARIABLE: GrentonActionVariable,
}


@dataclass
class GrentonEntitySceneConfigurationSchema(BaseGrentonEntityConfigurationSchema):
    """Show the effective action, then its type-specific arguments."""

    clu_options: list[dict[str, str]]

    @property
    def steps(self) -> list[StepDefinition]:
        return [
            StepDefinition("configure_scene_action", self._build_action),
            StepDefinition("configure_scene_arguments", self._build_arguments),
        ]

    def _build_action(self, current: dict[str, Any], accumulated: dict[str, Any]) -> StepResult:
        return StepResult(
            schema=vol.Schema({
                vol.Required("call_type", default=current["call_type"]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[call_type.value for call_type in GrentonActionCallType],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="scene_call_types",
                    )
                ),
                vol.Required("clu_id", default=current["clu_id"]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self.clu_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required("object_name", default=current["object_name"]): selector.TextSelector(),
            }),
            validator=self._validate_action,
        )

    def _validate_action(self, data: dict[str, Any]) -> dict[str, Any]:
        data = {**data, "object_name": data["object_name"].strip()}
        # Variable calls use only the variable index, so imported object names
        # may legitimately be empty for this call type.
        if data["call_type"] != GrentonActionCallType.VARIABLE and not data["object_name"]:
            raise vol.Invalid("Object name cannot be blank", path=["object_name"])
        return data

    def _build_arguments(self, current: dict[str, Any], accumulated: dict[str, Any]) -> StepResult:
        call_type = accumulated.get("call_type", current["call_type"])
        fields: dict[Any, Any] = {}
        if call_type != GrentonActionCallType.SCRIPT:
            fields[vol.Required("index", default=current.get("index", ""))] = selector.TextSelector()
        # A suggested value lets a cleared/omitted field mean no arguments rather
        # than having voluptuous silently reinsert the previous default.
        fields[vol.Optional("value", description={"suggested_value": current["value"]})] = (
            selector.TextSelector(selector.TextSelectorConfig(multiline=True))
        )
        return StepResult(
            schema=vol.Schema(fields),
            step_id=(
                "configure_scene_script_arguments"
                if call_type == GrentonActionCallType.SCRIPT
                else "configure_scene_arguments"
            ),
            validator=(
                vol.Schema({
                    vol.Required("index"): vol.All(str, vol.Strip, vol.Length(min=1)),
                }, extra=vol.ALLOW_EXTRA)
                if call_type != GrentonActionCallType.SCRIPT else None
            ),
        )


class GrentonEntitySceneButton(  # pyright: ignore[reportIncompatibleVariableOverride]
    BaseGrentonEntity,
    ConfigurableEntity[GrentonEntitySceneConfigurationSchema],
    ButtonEntity,
):
    """Scene button with an inspectable, configurable Grenton action."""

    def __init__(
        self,
        coordinator: GrentonCoordinator,
        id: str,
        label: str,
        script_action: GrentonAction,
        device_info: DeviceInfo | None = None,
    ) -> None:
        ButtonEntity.__init__(self)
        BaseGrentonEntity.__init__(self, coordinator, id, label, device_info)
        self._widget_action = script_action
        clu_options = [{"value": clu.id, "label": f"{clu.name} ({clu.id})"} for clu in coordinator.clus]
        ConfigurableEntity.__init__(self, clu_options=clu_options)
        self._config = {**self.default_config(), **self._config}
        for clu_id in {script_action.clu_id, self._config["clu_id"]}:
            if clu_id not in {option["value"] for option in clu_options}:
                clu_options.append({"value": clu_id, "label": clu_id})

    def default_config(self) -> dict[str, Any]:
        action = self._widget_action
        call_type = next(key for key, cls in ACTION_CLASSES.items() if isinstance(action, cls))
        config = {
            "call_type": call_type.value,
            "clu_id": action.clu_id,
            "object_name": action.object_name,
            "value": action.value,
        }
        if isinstance(action, (GrentonActionAttribute, GrentonActionMethod, GrentonActionVariable)):
            config["index"] = action.index
        return config

    @property
    def script_action(self) -> GrentonAction:
        """Effective action, including saved Home Assistant overrides."""
        config = self._config
        call_type = GrentonActionCallType(config["call_type"])
        fields = {
            "clu_id": config["clu_id"],
            "object_name": config["object_name"],
            "event": self._widget_action.event,
            "value": config["value"],
        }
        if call_type != GrentonActionCallType.SCRIPT:
            fields["index"] = config["index"]
        return ACTION_CLASSES[call_type](**fields)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose call details in Home Assistant's entity attributes."""
        action = self.script_action
        attributes = {
            **self._config,
            "event": action.event.value,
            "call": GrentonCluApiActionRequest.from_action(action).payload,
        }
        if isinstance(action, GrentonActionScript):
            attributes.pop("index", None)
        return attributes

    async def apply_configuration(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Apply immediately and return options to persist without a reload."""
        config = {**user_input, "value": user_input.get("value", "")}
        if config["call_type"] == GrentonActionCallType.SCRIPT:
            config.pop("index", None)
        options = await super().apply_configuration(config)
        if self.hass is not None and self.entity_id is not None:
            self.async_write_ha_state()
        return options

    async def async_press(self, **kwargs: Any) -> None:
        """Run the scene with its configured arguments/value."""
        await self.coordinator.execute_action(self.script_action)

    async def run_with_parameter(self, parameter: str | None = None) -> None:
        """Override arguments/value for one call without changing saved options.

        Scripts accept verbatim Lua arguments. Method, attribute and variable
        actions retain the protocol's quoted string value semantics.
        An omitted parameter uses the saved value; an empty string clears it.
        """
        action = self.script_action
        if parameter is not None:
            action = replace(action, value=parameter)
        await self.coordinator.execute_action(action)
