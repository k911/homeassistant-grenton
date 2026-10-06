"""Scene configuration, flow validation, and action execution regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from homeassistant.helpers import config_validation as cv
from probatio import to_field_list

from custom_components.homeassistant_grenton.domain.action import (
    GrentonActionAttribute,
    GrentonActionMethod,
    GrentonActionScript,
    GrentonActionVariable,
)
from custom_components.homeassistant_grenton.domain.entities.binary_sensor import (
    GrentonEntityBinarySensor,
)
from custom_components.homeassistant_grenton.domain.entities.scene_button import (
    GrentonEntitySceneButton,
)
from custom_components.homeassistant_grenton.domain.enums import GrentonActionEventType
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
        clus=[SimpleNamespace(id="clu1", name="Main CLU"), SimpleNamespace(id="clu2", name="Other CLU")],
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
    entity = GrentonEntitySceneButton(coordinator, "scene_1", "Evening", action_cls(**fields))
    entity.entity_id = "button.evening"
    return entity


def make_flow(coordinator, entity):
    coordinator.config_entry.runtime_data = SimpleNamespace(devices=[SimpleNamespace(entities=[entity])])
    flow = GrentonOptionsFlow()
    return flow, patch.object(
        GrentonOptionsFlow, "config_entry", new_callable=PropertyMock,
        return_value=coordinator.config_entry,
    )


def ui_fields(result):
    """Use the same schema serializer as Home Assistant's configuration UI."""
    return {field["name"]: field for field in to_field_list(
        result["data_schema"], custom_serializer=cv.custom_serializer,
    )}


@pytest.mark.parametrize(("action_cls", "call_type", "payload"), [
    (GrentonActionScript, "SCRIPT", 'Evening(42, "abc")'),
    (GrentonActionMethod, "METHOD", 'Evening:execute(7,"42")'),
    (GrentonActionAttribute, "ATTRIBUTE", 'Evening:set(7,"42")'),
    (GrentonActionVariable, "VARIABLE", 'setVar("7","42")'),
])
def test_imported_actions_are_visible_and_executable(coordinator, action_cls, call_type, payload):
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


def test_dialog_prefills_script_settings_and_serializes_for_ui(coordinator):
    entity = make_scene(coordinator)
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            result = await flow.async_step_init()
            assert "button.evening" in ui_fields(result)["entity"]["selector"]["entity"]["include_entities"]
            result = await flow.async_step_entity_list({"entity": "button.evening"})
            assert result["step_id"] == "configure_scene_action"
            fields = ui_fields(result)
            assert fields["call_type"]["default"] == "SCRIPT"
            assert fields["clu_id"]["default"] == "clu1"
            assert fields["object_name"]["default"] == "Evening"
            result = await flow.async_step_configure_scene_action({
                "call_type": "SCRIPT", "clu_id": "clu1", "object_name": "Evening",
            })
            assert result["step_id"] == "configure_scene_script_arguments"
            fields = ui_fields(result)
            assert "index" not in fields
            assert fields["value"]["description"]["suggested_value"] == '42, "abc"'

    asyncio.run(run())


def test_edit_save_reload_and_runtime_parameter_do_not_mutate_defaults(coordinator):
    entity = make_scene(coordinator)
    entity.hass = Mock()
    entity.async_write_ha_state = Mock()
    coordinator.config_entry.options = {"unrelated": {"keep": True}, "entities": {"other": {"unit": "W"}}}
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            await flow.async_step_entity_list({"entity": "button.evening"})
            await flow.async_step_configure_scene_action({
                "call_type": "METHOD", "clu_id": "clu2", "object_name": " Relay ",
            })
            result = await flow.async_step_configure_scene_arguments({"index": " 3 ", "value": "80"})
            assert result["type"] == "create_entry"
            assert result["data"]["unrelated"] == {"keep": True}
            assert result["data"]["entities"]["other"] == {"unit": "W"}
            assert entity.extra_state_attributes["call"] == 'Relay:execute(3,"80")'
            entity.async_write_ha_state.assert_called_once()
            # Emulate Home Assistant committing the options, then re-creating
            # the entity from a freshly fetched interface with different defaults.
            coordinator.config_entry.options = result["data"]
            restored = make_scene(coordinator, value="99")
            assert restored.extra_state_attributes == entity.extra_state_attributes
            await restored.async_press()
            await restored.run_with_parameter("20")
            await restored.run_with_parameter("")
            await restored.run_with_parameter()
            assert [call.args[0].value for call in coordinator.execute_action.await_args_list] == ["80", "20", "", "80"]
            assert restored.extra_state_attributes["value"] == "80"
            assert entity._widget_action.value == '42, "abc"'
            assert coordinator.config_entry.options == result["data"]

    asyncio.run(run())


@pytest.mark.parametrize("submitted", [{}, {"value": ""}])
def test_clearing_script_arguments_persists_an_empty_call(coordinator, submitted):
    entity = make_scene(coordinator)
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            await flow.async_step_entity_list({"entity": "button.evening"})
            await flow.async_step_configure_scene_action({
                "call_type": "SCRIPT", "clu_id": "clu1", "object_name": "Evening",
            })
            result = await flow.async_step_configure_scene_script_arguments(submitted)
            assert result["data"]["entities"]["scene_1"]["value"] == ""
            assert entity.extra_state_attributes["call"] == "Evening()"
            coordinator.config_entry.options = result["data"]
            assert make_scene(coordinator).extra_state_attributes["call"] == "Evening()"

    asyncio.run(run())


@pytest.mark.parametrize(("field", "value"), [("object_name", "  "), ("call_type", "INVALID"), ("clu_id", "missing")])
def test_invalid_action_settings_keep_dialog_open(coordinator, field, value):
    entity = make_scene(coordinator)
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            await flow.async_step_entity_list({"entity": "button.evening"})
            submitted = {"call_type": "SCRIPT", "clu_id": "clu1", "object_name": "Evening", field: value}
            result = await flow.async_step_configure_scene_action(submitted)
            assert result["step_id"] == "configure_scene_action"
            assert result["errors"] == {field: "invalid_configuration"}
            assert flow.current_step_index == 0
            assert entity.extra_state_attributes["call"] == 'Evening(42, "abc")'
            assert ui_fields(result)[field]["default"] == value

    asyncio.run(run())


def test_switching_script_to_method_requires_index_and_recovers(coordinator):
    entity = make_scene(coordinator)
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            await flow.async_step_entity_list({"entity": "button.evening"})
            result = await flow.async_step_configure_scene_action({
                "call_type": "METHOD", "clu_id": "clu1", "object_name": "Relay",
            })
            assert "index" in ui_fields(result)
            result = await flow.async_step_configure_scene_arguments({"value": "10"})
            assert result["errors"] == {"index": "invalid_configuration"}
            assert ui_fields(result)["value"]["description"]["suggested_value"] == "10"
            assert flow.current_step_index == 1
            result = await flow.async_step_configure_scene_arguments({"index": "4", "value": "10"})
            assert result["type"] == "create_entry"
            assert entity.extra_state_attributes["call"] == 'Relay:execute(4,"10")'

    asyncio.run(run())


def test_switching_method_to_script_removes_index(coordinator):
    entity = make_scene(coordinator, GrentonActionMethod)
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            await flow.async_step_entity_list({"entity": "button.evening"})
            await flow.async_step_configure_scene_action({
                "call_type": "SCRIPT", "clu_id": "clu1", "object_name": "Night",
            })
            result = await flow.async_step_configure_scene_script_arguments({"value": '1, "bedroom"'})
            assert "index" not in result["data"]["entities"]["scene_1"]
            assert entity.extra_state_attributes["call"] == 'Night(1, "bedroom")'
            coordinator.config_entry.options = result["data"]
            restored = make_scene(coordinator, GrentonActionMethod)
            assert "index" not in restored.extra_state_attributes

    asyncio.run(run())


@pytest.mark.parametrize("call_type", ["SCRIPT", "METHOD", "ATTRIBUTE", "VARIABLE"])
def test_scene_mapper_imports_click_actions(coordinator, call_type):
    action = {"event": "CLICK", "cluId": "clu1", "objectName": "Scene", "value": "1", "callType": call_type}
    if call_type != "SCRIPT":
        action["index"] = "2"
    dto = GrentonWidgetSceneDto(id="scene", components=[{
        "label": "Run scene", "rowId": 3, "unit": "UNKNOWN",
        "actions": [{**action, "event": "ON"}, action],
    }, {"label": "No click", "rowId": 4, "unit": "UNKNOWN", "actions": []}])
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
            result = await flow.async_step_configure_binary_sensor_class({"device_class": "door"})
            assert result["data"]["entities"]["sensor_1"] == {"device_class": "door"}
            assert entity.device_class == "door"

    asyncio.run(run())


def test_mapper_keeps_existing_script_preference(coordinator):
    common = {"event": "CLICK", "cluId": "clu1", "objectName": "Scene", "value": "1"}
    dto = GrentonWidgetSceneDto(id="scene", components=[{
        "label": "Run scene", "rowId": 3, "unit": "UNKNOWN", "actions": [
            {**common, "callType": "METHOD", "index": "2"},
            {**common, "callType": "SCRIPT"},
        ],
    }])
    entity = DeviceSceneMapper.to_domain(dto, coordinator).entities[0]
    assert entity.extra_state_attributes["call"] == "Scene(1)"


def test_variable_dialog_accepts_an_empty_object_name(coordinator):
    entity = make_scene(coordinator, GrentonActionVariable, object_name="")
    flow, entry_patch = make_flow(coordinator, entity)

    async def run():
        with entry_patch:
            await flow.async_step_entity_list({"entity": "button.evening"})
            result = await flow.async_step_configure_scene_action({
                "call_type": "VARIABLE", "clu_id": "clu1", "object_name": "",
            })
            assert result["step_id"] == "configure_scene_arguments"
            assert ui_fields(result)["index"]["default"] == "7"
            assert ui_fields(result)["value"]["description"]["suggested_value"] == "42"
            result = await flow.async_step_configure_scene_arguments({"index": "7", "value": "10"})
            assert result["type"] == "create_entry"
            assert entity.extra_state_attributes["call"] == 'setVar("7","10")'

    asyncio.run(run())
