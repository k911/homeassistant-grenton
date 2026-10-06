"""One native Home Assistant form for the complete scene action."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

from ..enums import GrentonActionCallType
from ..scene_arguments import arguments_to_ui, imported_arguments, normalize_arguments
from .configurable import (
    BaseGrentonEntityConfigurationSchema,
    StepDefinition,
    StepResult,
)


def scene_arguments_selector() -> selector.ObjectSelector:
    """Repeatable rows with a type picker and an appropriate value control."""
    type_selectors = {
        "string": selector.TextSelector(),
        "number": selector.NumberSelector(
            selector.NumberSelectorConfig(
                mode=selector.NumberSelectorMode.BOX,
                step="any",
            )
        ),
        "boolean": selector.BooleanSelector(),
        "nil": selector.ConstantSelector(selector.ConstantSelectorConfig(value="nil")),
        "lua": selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
    }
    return selector.ObjectSelector(
        selector.ObjectSelectorConfig(
            multiple=True,
            translation_key="scene_arguments",
            fields={
                "argument": {
                    "required": True,
                    "selector": selector.ChooseSelector(
                        selector.ChooseSelectorConfig(
                            translation_key="scene_argument_types",
                            # ChooseSelector validates choices by constructing
                            # selectors from their serialized definitions.
                            choices={
                                name: {"selector": item.serialize()["selector"]}
                                for name, item in type_selectors.items()
                            },
                        )
                    ),
                },
            },
        )
    )


def validate_scene_action(data: dict[str, Any]) -> dict[str, Any]:
    """Validate the applicable fields and preserve explicitly empty arguments."""
    config = dict(data)
    call_type = config["call_type"]
    config["object_name"] = config.get("object_name", "").strip()
    if call_type != "VARIABLE" and not config["object_name"]:
        raise vol.Invalid("Enter a script/object name", path=["object_name"])
    if call_type != "SCRIPT":
        config["index"] = config.get("index", "").strip()
        if not config["index"]:
            raise vol.Invalid("Enter a method/attribute/variable index", path=["index"])
    if call_type in ("SCRIPT", "METHOD"):
        try:
            config["arguments"] = normalize_arguments(config.get("arguments", []))
        except (TypeError, ValueError) as err:
            raise vol.Invalid(str(err), path=["arguments"]) from err
        config.pop("value", None)
    else:
        config.pop("arguments", None)
        config["value"] = config.get("value", "")
    return config


@dataclass
class GrentonEntitySceneConfigurationSchema(BaseGrentonEntityConfigurationSchema):
    """Keep the call type, target, index and argument list in one dialog."""

    clu_options: list[dict[str, str]]

    @property
    def steps(self) -> list[StepDefinition]:
        return [StepDefinition("configure_scene_action", self._build_action)]

    def _build_action(
        self, current: dict[str, Any], accumulated: dict[str, Any]
    ) -> StepResult:
        arguments = current.get(
            "arguments", imported_arguments(current["call_type"], current["value"])
        )
        # Typed editor values on a validation error are already in UI format.
        if isinstance(arguments, list) and all(
            isinstance(row, dict) and "type" in row for row in arguments
        ):
            arguments = arguments_to_ui(arguments)
        else:
            # Preserve incomplete/invalid editor rows so they can be corrected.
            arguments = deepcopy(arguments)
        return StepResult(
            schema=vol.Schema(
                {
                    vol.Required(
                        "call_type", default=current["call_type"]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[item.value for item in GrentonActionCallType],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key="scene_call_types",
                        ),
                    ),
                    vol.Required(
                        "clu_id", default=current["clu_id"]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self.clu_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                    vol.Optional(
                        "object_name",
                        description={"suggested_value": current["object_name"]},
                    ): selector.TextSelector(),
                    vol.Optional(
                        "index",
                        description={"suggested_value": current.get("index", "")},
                    ): selector.TextSelector(),
                    vol.Optional(
                        "arguments", description={"suggested_value": arguments}
                    ): scene_arguments_selector(),
                    vol.Optional(
                        "value", description={"suggested_value": current["value"]}
                    ): selector.TextSelector(),
                }
            ),
            validator=validate_scene_action,
        )
