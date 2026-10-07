from dataclasses import replace
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
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
from ..scene_arguments import (
    argument_expressions,
    imported_arguments,
    normalize_arguments,
)
from .base import BaseGrentonEntity
from .configurable import ConfigurableEntity
from .scene_configuration import GrentonEntitySceneConfigurationSchema

ACTION_CLASSES = {
    GrentonActionCallType.ATTRIBUTE: GrentonActionAttribute,
    GrentonActionCallType.METHOD: GrentonActionMethod,
    GrentonActionCallType.SCRIPT: GrentonActionScript,
    GrentonActionCallType.VARIABLE: GrentonActionVariable,
}


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
        clu_options = [
            {"value": clu.id, "label": f"{clu.name} ({clu.id})"}
            for clu in coordinator.clus
        ]
        ConfigurableEntity.__init__(self, clu_options=clu_options)
        loaded = self._config
        self._config = {**self.default_config(), **loaded}
        if self._config["call_type"] == "VARIABLE":
            # Migrate the old variable "index" to its actual string name.
            self._config["variable_name"] = loaded.get(
                "variable_name",
                loaded.get("index", self._config.get("variable_name", "")),
            )
            self._config.pop("index", None)
            self._config.pop("object_name", None)
        else:
            self._config.pop("variable_name", None)
        if self._config["call_type"] == "SCRIPT":
            self._config.pop("index", None)
        for clu_id in {script_action.clu_id, self._config["clu_id"]}:
            if clu_id not in {option["value"] for option in clu_options}:
                clu_options.append({"value": clu_id, "label": clu_id})

    def default_config(self) -> dict[str, Any]:
        action = self._widget_action
        call_type = next(
            key for key, cls in ACTION_CLASSES.items() if isinstance(action, cls)
        )
        config = {
            "call_type": call_type.value,
            "clu_id": action.clu_id,
            "object_name": action.object_name,
            "value": action.value,
        }
        if isinstance(action, GrentonActionVariable):
            config["variable_name"] = action.index
            config.pop("object_name")
        elif isinstance(action, (GrentonActionAttribute, GrentonActionMethod)):
            config["index"] = action.index
        return config

    @property
    def script_action(self) -> GrentonAction:
        """Effective action, including saved Home Assistant overrides."""
        config = self._config
        call_type = GrentonActionCallType(config["call_type"])
        fields = {
            "clu_id": config["clu_id"],
            "object_name": config.get("object_name", ""),
            "event": self._widget_action.event,
            "value": config["value"],
        }
        if "arguments" in config and call_type in (
            GrentonActionCallType.SCRIPT,
            GrentonActionCallType.METHOD,
        ):
            expressions = argument_expressions(config["arguments"])
            fields["value"] = ", ".join(expressions)
            if call_type == GrentonActionCallType.METHOD:
                fields["arguments"] = expressions
        if call_type == GrentonActionCallType.VARIABLE:
            fields["index"] = config["variable_name"]
        elif call_type != GrentonActionCallType.SCRIPT:
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
        if isinstance(action, (GrentonActionScript, GrentonActionMethod)):
            attributes["arguments"] = self._config.get(
                "arguments",
                imported_arguments(self._config["call_type"], self._config["value"]),
            )
        if isinstance(action, GrentonActionScript):
            attributes.pop("index", None)
        return attributes

    async def apply_configuration(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Apply immediately and return options to persist without a reload."""
        config = {**user_input, "value": user_input.get("value", "")}
        if config["call_type"] in (
            GrentonActionCallType.SCRIPT,
            GrentonActionCallType.METHOD,
        ):
            if "arguments" in config:
                config["arguments"] = normalize_arguments(config["arguments"])
                config["value"] = ", ".join(argument_expressions(config["arguments"]))
        else:
            config.pop("arguments", None)
        if config["call_type"] == GrentonActionCallType.SCRIPT:
            config.pop("index", None)
        if config["call_type"] == GrentonActionCallType.VARIABLE:
            config["variable_name"] = config.get(
                "variable_name", config.get("index", "")
            )
            config.pop("index", None)
            config.pop("object_name", None)
        else:
            config.pop("variable_name", None)
        options = await super().apply_configuration(config)
        if self.hass is not None and self.entity_id is not None:
            self.async_write_ha_state()
        return options

    async def async_press(self, **kwargs: Any) -> None:
        """Run the scene with its configured arguments/value."""
        await self.coordinator.execute_action(self.script_action)

    async def run_with_parameter(
        self,
        parameter: str | None = None,
        arguments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Override arguments/value for one call without changing saved options.

        The legacy parameter keeps its existing semantics. Typed arguments
        override the argument list for a script/method without changing options.
        Omitted fields use the saved settings; arguments=[] calls without args.
        """
        action = self.script_action
        if parameter is not None and arguments is not None:
            raise HomeAssistantError("Use either parameter or arguments, not both")
        if arguments is not None:
            if not isinstance(action, (GrentonActionScript, GrentonActionMethod)):
                raise HomeAssistantError(
                    "Typed arguments require a script or method call"
                )
            try:
                expressions = argument_expressions(arguments)
            except (TypeError, ValueError) as err:
                raise HomeAssistantError(str(err)) from err
            action = replace(action, value=", ".join(expressions))
            if isinstance(action, GrentonActionMethod):
                action = replace(action, arguments=expressions)
        if parameter is not None:
            action = replace(action, value=parameter)
            if isinstance(action, GrentonActionMethod):
                action = replace(action, arguments=None)
        await self.coordinator.execute_action(action)
