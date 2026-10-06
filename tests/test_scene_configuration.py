"""Scene editor schemas, typed arguments, persistence and wire regressions."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
import yaml
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from probatio import Schema, to_field_list

from custom_components.homeassistant_grenton.button import SERVICE_RUN_SCENE_SCHEMA
from custom_components.homeassistant_grenton.domain.action import (
    GrentonActionAttribute,
    GrentonActionMethod,
    GrentonActionScript,
    GrentonActionVariable,
)
from custom_components.homeassistant_grenton.domain.api.clu_messages.action import (
    GrentonCluApiActionRequest,
)
from custom_components.homeassistant_grenton.domain.entities.binary_sensor import (
    GrentonEntityBinarySensor,
)
from custom_components.homeassistant_grenton.domain.entities.scene_button import (
    GrentonEntitySceneButton,
)
from custom_components.homeassistant_grenton.domain.enums import GrentonActionEventType
from custom_components.homeassistant_grenton.domain.scene_arguments import (
    ARGUMENT_TYPES,
    argument_expressions,
    arguments_to_ui,
)
from custom_components.homeassistant_grenton.dto.widgets.scene import (
    GrentonWidgetSceneDto,
)
from custom_components.homeassistant_grenton.mappers.device_scene import (
    DeviceSceneMapper,
)
from custom_components.homeassistant_grenton.options_flow import GrentonOptionsFlow


@pytest.fixture
def coordinator():
    return SimpleNamespace(
        config_entry=SimpleNamespace(options={}),
        clus=[
            SimpleNamespace(id="clu1", name="Main CLU"),
            SimpleNamespace(id="clu2", name="Other CLU"),
        ],
        execute_action=AsyncMock(),
        register_component_state=Mock(),
    )


def make_scene(coordinator, action_cls=GrentonActionScript, **overrides):
    fields = {
        "clu_id": "clu1",
        "object_name": "Evening",
        "event": GrentonActionEventType.CLICK,
        "value": '42, "abc"' if action_cls is GrentonActionScript else "42",
        **overrides,
    }
    if action_cls is not GrentonActionScript:
        fields.setdefault("index", "7")
    entity = GrentonEntitySceneButton(
        coordinator, "scene_1", "Evening", action_cls(**fields)
    )
    entity.entity_id = "button.evening"
    return entity


def make_flow(coordinator, entity):
    coordinator.config_entry.runtime_data = SimpleNamespace(
        devices=[SimpleNamespace(entities=[entity])]
    )
    return GrentonOptionsFlow(), patch.object(
        GrentonOptionsFlow,
        "config_entry",
        new_callable=PropertyMock,
        return_value=coordinator.config_entry,
    )


def ui_fields(result):
    return {
        field["name"]: field
        for field in to_field_list(
            result["data_schema"],
            custom_serializer=cv.custom_serializer,
        )
    }


def editor_input(call_type="SCRIPT", arguments=None, **overrides):
    return {
        "call_type": call_type,
        "clu_id": "clu1",
        "object_name": "Evening",
        "index": "7",
        "arguments": arguments_to_ui(arguments or []),
        **overrides,
    }


async def save_scene(coordinator, entity, submitted):
    flow, entry_patch = make_flow(coordinator, entity)
    with entry_patch:
        await flow.async_step_entity_list({"entity": entity.entity_id})
        return await flow.async_step_configure_scene_action(submitted)


@pytest.mark.parametrize(
    ("action_cls", "call_type", "payload"),
    [
        (GrentonActionScript, "SCRIPT", 'Evening(42, "abc")'),
        (GrentonActionMethod, "METHOD", 'Evening:execute(7,"42")'),
        (GrentonActionAttribute, "ATTRIBUTE", 'Evening:set(7,"42")'),
        (GrentonActionVariable, "VARIABLE", 'setVar("7","42")'),
    ],
)
def test_imported_actions_are_visible_and_executable(
    coordinator, action_cls, call_type, payload
):
    entity = make_scene(coordinator, action_cls)
    attributes = entity.extra_state_attributes
    assert attributes["call_type"] == call_type
    assert attributes["clu_id"] == "clu1"
    assert attributes["object_name"] == "Evening"
    assert attributes["event"] == "CLICK"
    assert attributes["call"] == payload
    assert ("index" in attributes) == (call_type != "SCRIPT")
    asyncio.run(entity.async_press())
    assert coordinator.execute_action.await_args.args[0] == entity.script_action


@pytest.mark.parametrize("action_cls", [GrentonActionScript, GrentonActionMethod])
def test_all_settings_and_dynamic_typed_argument_controls_share_one_dialog(
    coordinator, action_cls
):
    entity = make_scene(coordinator, action_cls)
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            result = await flow.async_step_init()
            assert (
                "button.evening"
                in ui_fields(result)["entity"]["selector"]["entity"]["include_entities"]
            )
            result = await flow.async_step_entity_list({"entity": entity.entity_id})
            assert result["step_id"] == "configure_scene_action"
            fields = ui_fields(result)
            assert set(fields) == {
                "call_type",
                "clu_id",
                "object_name",
                "index",
                "arguments",
                "value",
            }
            assert fields["call_type"]["selector"]["select"]["mode"] == "dropdown"
            assert set(fields["call_type"]["selector"]["select"]["options"]) == {
                "SCRIPT",
                "METHOD",
                "ATTRIBUTE",
                "VARIABLE",
            }
            assert (
                fields["call_type"]["default"]
                == entity.extra_state_attributes["call_type"]
            )
            assert fields["clu_id"]["default"] == "clu1"
            assert fields["object_name"]["description"]["suggested_value"] == "Evening"
            object_config = fields["arguments"]["selector"]["object"]
            assert object_config["multiple"] is True
            choices = object_config["fields"]["argument"]["selector"]["choose"][
                "choices"
            ]
            assert set(choices) == {"string", "number", "boolean", "nil", "lua"}
            assert choices["number"]["selector"]["number"]["step"] == "any"
            assert "boolean" in choices["boolean"]["selector"]
            initial_rows = fields["arguments"]["description"]["suggested_value"]
            submitted = editor_input(
                fields["call_type"]["default"],
                arguments=entity.extra_state_attributes["arguments"],
            )
            assert submitted["arguments"] == initial_rows
            result = await flow.async_step_configure_scene_action(submitted)
            assert result["type"] == "create_entry"  # No second dialog.

    asyncio.run(run())


@pytest.mark.parametrize(
    ("call_type", "prefix"), [("SCRIPT", "Evening("), ("METHOD", "Evening:execute(7, ")]
)
def test_argument_types_are_encoded_in_order_and_persist(
    coordinator, call_type, prefix
):
    entity = make_scene(coordinator)
    arguments = [
        {"type": "number", "value": -1.25},
        {"type": "string", "value": 'room "A"\\door\nnext'},
        {"type": "boolean", "value": False},
        {"type": "nil", "value": None},
        {"type": "lua", "value": "OTHER:get(2)"},
    ]
    result = asyncio.run(
        save_scene(coordinator, entity, editor_input(call_type, arguments))
    )
    expected = (
        prefix + '-1.25, "room \\"A\\"\\\\door\\nnext", false, nil, OTHER:get(2))'
    )
    assert result["type"] == "create_entry"
    assert entity.extra_state_attributes["call"] == expected
    assert result["data"]["entities"]["scene_1"]["arguments"] == arguments
    coordinator.config_entry.options = result["data"]
    restored = make_scene(coordinator, value="99")
    assert restored.extra_state_attributes == entity.extra_state_attributes
    asyncio.run(restored.async_press())
    assert (
        GrentonCluApiActionRequest.from_action(
            coordinator.execute_action.await_args.args[0]
        ).payload
        == expected
    )


def test_save_updates_state_and_keeps_unrelated_options(coordinator):
    entity = make_scene(coordinator)
    entity.hass = Mock()
    entity.async_write_ha_state = Mock()
    coordinator.config_entry.options = {
        "unrelated": {"keep": True},
        "entities": {"other": {"unit": "W"}},
    }
    result = asyncio.run(
        save_scene(
            coordinator,
            entity,
            editor_input(
                "METHOD",
                [{"type": "number", "value": 80}],
                clu_id="clu2",
                object_name=" Relay ",
                index=" 3 ",
            ),
        )
    )
    assert result["data"]["unrelated"] == {"keep": True}
    assert result["data"]["entities"]["other"] == {"unit": "W"}
    assert entity.extra_state_attributes["call"] == "Relay:execute(3, 80)"
    entity.async_write_ha_state.assert_called_once()
    assert entity._widget_action.value == '42, "abc"'


@pytest.mark.parametrize(
    ("call_type", "expected"),
    [("SCRIPT", "Evening()"), ("METHOD", "Evening:execute(7)")],
)
@pytest.mark.parametrize("omit_arguments", [False, True])
def test_removing_all_arguments_does_not_reinsert_old_defaults(
    coordinator, call_type, expected, omit_arguments
):
    entity = make_scene(coordinator)
    submitted = editor_input(call_type)
    if omit_arguments:
        submitted.pop("arguments")
    result = asyncio.run(save_scene(coordinator, entity, submitted))
    assert result["data"]["entities"]["scene_1"]["arguments"] == []
    assert entity.extra_state_attributes["call"] == expected
    coordinator.config_entry.options = result["data"]
    assert make_scene(coordinator).extra_state_attributes["call"] == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_name", "  "),
        ("call_type", "INVALID"),
        ("clu_id", "missing"),
        ("index", ""),
    ],
)
def test_invalid_settings_keep_the_combined_dialog_open(coordinator, field, value):
    entity = make_scene(coordinator)
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            await flow.async_step_entity_list({"entity": entity.entity_id})
            submitted = editor_input("METHOD")
            submitted[field] = value
            result = await flow.async_step_configure_scene_action(submitted)
            assert result["step_id"] == "configure_scene_action"
            assert result["errors"] == {field: "invalid_configuration"}
            assert flow.current_step_index == 0
            assert entity.extra_state_attributes["call"] == 'Evening(42, "abc")'
            fields = ui_fields(result)
            assert (
                fields[field].get(
                    "default",
                    fields[field].get("description", {}).get("suggested_value"),
                )
                == value
            )
            result = await flow.async_step_configure_scene_action(
                editor_input("METHOD", index="4")
            )
            assert result["type"] == "create_entry"
            assert entity.extra_state_attributes["call"] == "Evening:execute(4)"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("argument_type", "value"),
    [
        ("lua", ""),
        ("number", "NaN"),
        ("number", "Infinity"),
        ("number", "not numeric"),
        ("boolean", "false"),
        ("string", 42),
        ("nil", "invalid"),
        ("unknown", 1),
    ],
)
def test_invalid_arguments_are_retained_for_correction(
    coordinator, argument_type, value
):
    entity = make_scene(coordinator)
    flow, entry_patch = make_flow(coordinator, entity)
    rows = [{"argument": {"active_choice": argument_type, argument_type: value}}]

    async def run():
        with entry_patch:
            await flow.async_step_entity_list({"entity": entity.entity_id})
            submitted = editor_input()
            submitted["arguments"] = rows
            result = await flow.async_step_configure_scene_action(submitted)
            assert result["errors"] == {"arguments": "invalid_configuration"}
            assert (
                ui_fields(result)["arguments"]["description"]["suggested_value"] == rows
            )
            assert entity.extra_state_attributes["call"] == 'Evening(42, "abc")'
            result = await flow.async_step_configure_scene_action(
                editor_input(arguments=[{"type": "string", "value": "fixed"}])
            )
            assert result["type"] == "create_entry"
            assert entity.extra_state_attributes["call"] == 'Evening("fixed")'

    asyncio.run(run())


def test_rows_can_be_added_removed_and_reordered_on_reopen(coordinator):
    entity = make_scene(coordinator)
    original = [
        {"type": "number", "value": 1},
        {"type": "string", "value": "remove"},
        {"type": "boolean", "value": True},
    ]
    result = asyncio.run(
        save_scene(coordinator, entity, editor_input(arguments=original))
    )
    coordinator.config_entry.options = result["data"]
    entity = make_scene(coordinator)
    changed = [original[2], original[0], {"type": "number", "value": 3}]
    result = asyncio.run(
        save_scene(coordinator, entity, editor_input(arguments=changed))
    )
    assert entity.extra_state_attributes["call"] == "Evening(true, 1, 3)"
    assert result["data"]["entities"]["scene_1"]["arguments"] == changed


@pytest.mark.parametrize("call_type", ["SCRIPT", "METHOD"])
def test_legacy_and_typed_runtime_overrides_do_not_change_saved_arguments(
    coordinator, call_type
):
    entity = make_scene(coordinator)
    result = asyncio.run(
        save_scene(
            coordinator,
            entity,
            editor_input(call_type, [{"type": "number", "value": 80}]),
        )
    )
    coordinator.config_entry.options = result["data"]
    initial = entity.extra_state_attributes

    async def run():
        await entity.run_with_parameter()
        await entity.run_with_parameter("20")
        await entity.run_with_parameter(arguments=[{"type": "boolean", "value": True}])
        await entity.run_with_parameter(arguments=[])
        await entity.run_with_parameter()

    asyncio.run(run())
    payloads = [
        GrentonCluApiActionRequest.from_action(call.args[0]).payload
        for call in coordinator.execute_action.await_args_list
    ]
    assert payloads == (
        ["Evening(80)", "Evening(20)", "Evening(true)", "Evening()", "Evening(80)"]
        if call_type == "SCRIPT"
        else [
            "Evening:execute(7, 80)",
            'Evening:execute(7,"20")',
            "Evening:execute(7, true)",
            "Evening:execute(7)",
            "Evening:execute(7, 80)",
        ]
    )
    assert entity.extra_state_attributes == initial
    assert coordinator.config_entry.options == result["data"]
    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.run_with_parameter(parameter="1", arguments=[]))


@pytest.mark.parametrize("action_cls", [GrentonActionAttribute, GrentonActionVariable])
def test_set_value_calls_use_value_and_ignore_stale_argument_rows(
    coordinator, action_cls
):
    entity = make_scene(coordinator, action_cls)
    call_type = entity.extra_state_attributes["call_type"]
    flow, entry_patch = make_flow(coordinator, entity)
    with entry_patch:
        fields = ui_fields(
            asyncio.run(flow.async_step_entity_list({"entity": entity.entity_id}))
        )
    assert fields["arguments"]["description"]["suggested_value"] == []
    result = asyncio.run(
        save_scene(
            coordinator,
            entity,
            editor_input(
                call_type,
                [{"type": "number", "value": 99}],
                value="10",
                object_name="" if call_type == "VARIABLE" else "Evening",
            ),
        )
    )
    assert result["type"] == "create_entry"
    assert "arguments" not in result["data"]["entities"]["scene_1"]
    assert entity.extra_state_attributes["call"] == (
        'setVar("7","10")' if call_type == "VARIABLE" else 'Evening:set(7,"10")'
    )
    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.run_with_parameter(arguments=[]))


def test_switching_to_script_removes_index(coordinator):
    entity = make_scene(coordinator, GrentonActionMethod)
    result = asyncio.run(
        save_scene(
            coordinator,
            entity,
            editor_input("SCRIPT", [{"type": "string", "value": "bedroom"}]),
        )
    )
    assert "index" not in result["data"]["entities"]["scene_1"]
    coordinator.config_entry.options = result["data"]
    restored = make_scene(coordinator, GrentonActionMethod)
    assert "index" not in restored.extra_state_attributes
    assert restored.extra_state_attributes["call"] == 'Evening("bedroom")'


def test_variable_call_does_not_require_an_unused_object_name(coordinator):
    entity = make_scene(coordinator)
    submitted = editor_input("VARIABLE", index="level", value="20")
    submitted.pop("object_name")
    result = asyncio.run(save_scene(coordinator, entity, submitted))
    assert result["type"] == "create_entry"
    assert entity.extra_state_attributes["call"] == 'setVar("level","20")'
    assert entity.extra_state_attributes["object_name"] == ""


@pytest.mark.parametrize("call_type", ["SCRIPT", "METHOD", "ATTRIBUTE", "VARIABLE"])
def test_scene_mapper_imports_click_actions(coordinator, call_type):
    action = {
        "event": "CLICK",
        "cluId": "clu1",
        "objectName": "Scene",
        "value": "1",
        "callType": call_type,
    }
    if call_type != "SCRIPT":
        action["index"] = "2"
    dto = GrentonWidgetSceneDto(
        id="scene",
        components=[
            {
                "label": "Run scene",
                "rowId": 3,
                "unit": "UNKNOWN",
                "actions": [{**action, "event": "ON"}, action],
            },
            {"label": "No click", "rowId": 4, "unit": "UNKNOWN", "actions": []},
        ],
    )
    device = DeviceSceneMapper.to_domain(dto, coordinator)
    assert len(device.entities) == 1
    assert device.entities[0].unique_id == "scene_3"
    assert device.entities[0].extra_state_attributes["call_type"] == call_type


def test_existing_binary_sensor_configuration_still_completes(coordinator):
    entity = GrentonEntityBinarySensor(coordinator, "sensor_1", "Door", False, Mock())
    entity.entity_id = "binary_sensor.door"
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            result = await flow.async_step_entity_list({"entity": entity.entity_id})
            assert result["step_id"] == "configure_binary_sensor_class"
            result = await flow.async_step_configure_binary_sensor_class(
                {"device_class": "door"}
            )
            assert result["data"]["entities"]["sensor_1"] == {"device_class": "door"}
            assert entity.device_class == "door"

    asyncio.run(run())


@pytest.mark.parametrize("call_type", ["SCRIPT", "METHOD"])
def test_service_editor_matches_registered_selector_and_accepts_typed_rows(
    coordinator, call_type
):
    component = Path(__file__).parents[1] / "custom_components/homeassistant_grenton"
    metadata = yaml.safe_load((component / "services.yaml").read_text())
    declared_selector = selector.selector(
        metadata["run_scene"]["fields"]["arguments"]["selector"]
    )
    registered_selector = next(
        field
        for key, field in SERVICE_RUN_SCENE_SCHEMA.items()
        if key.schema == "arguments"
    )
    assert declared_selector.serialize() == registered_selector.serialize()
    arguments = [
        {"type": "string", "value": "room"},
        {"type": "number", "value": 12.5},
        {"type": "boolean", "value": False},
        {"type": "nil", "value": None},
        {"type": "lua", "value": "other.value"},
    ]
    submitted = Schema(SERVICE_RUN_SCENE_SCHEMA)(
        {"arguments": arguments_to_ui(arguments)}
    )
    entity = make_scene(
        coordinator,
        GrentonActionScript if call_type == "SCRIPT" else GrentonActionMethod,
    )
    asyncio.run(entity.run_with_parameter(**submitted))
    assert GrentonCluApiActionRequest.from_action(
        coordinator.execute_action.await_args.args[0]
    ).payload == (
        'Evening("room", 12.5, false, nil, other.value)'
        if call_type == "SCRIPT"
        else 'Evening:execute(7, "room", 12.5, false, nil, other.value)'
    )


@pytest.mark.parametrize("language", ["en", "pl"])
def test_scene_editor_and_service_controls_have_translations(coordinator, language):
    component = Path(__file__).parents[1] / "custom_components/homeassistant_grenton"
    translations = json.loads((component / f"translations/{language}.json").read_text())
    entity = make_scene(coordinator)
    flow, entry_patch = make_flow(coordinator, entity)
    with entry_patch:
        fields = ui_fields(
            asyncio.run(flow.async_step_entity_list({"entity": entity.entity_id}))
        )
    assert set(
        translations["options"]["step"]["configure_scene_action"]["data"]
    ) == set(fields)
    assert set(translations["selector"]["scene_call_types"]["options"]) == {
        "SCRIPT",
        "METHOD",
        "ATTRIBUTE",
        "VARIABLE",
    }
    assert set(translations["selector"]["scene_argument_types"]["choices"]) == set(
        ARGUMENT_TYPES
    )
    assert translations["selector"]["scene_arguments"]["fields"]["argument"]["name"]
    assert set(translations["services"]["run_scene"]["fields"]) == {
        key.schema for key in SERVICE_RUN_SCENE_SCHEMA
    }


def test_typed_string_controls_are_escaped_and_integer_precision_is_preserved():
    assert argument_expressions(
        [
            {"type": "string", "value": "żółć\x007\x1b9\x7f"},
            {"type": "number", "value": 9007199254740993},
        ]
    ) == ('"żółć\\0007\\0279\\127"', "9007199254740993")


def test_mapper_keeps_existing_script_preference(coordinator):
    common = {"event": "CLICK", "cluId": "clu1", "objectName": "Scene", "value": "1"}
    dto = GrentonWidgetSceneDto(
        id="scene",
        components=[
            {
                "label": "Run scene",
                "rowId": 3,
                "unit": "UNKNOWN",
                "actions": [
                    {**common, "callType": "METHOD", "index": "2"},
                    {**common, "callType": "SCRIPT"},
                ],
            }
        ],
    )
    assert (
        DeviceSceneMapper.to_domain(dto, coordinator)
        .entities[0]
        .extra_state_attributes["call"]
        == "Scene(1)"
    )
