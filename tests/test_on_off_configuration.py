"""ON_OFF domain selection, options flow and native HA registration."""

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry, OptionsFlowManager
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from probatio import to_field_list

from custom_components.homeassistant_grenton import _cleanup_orphans
from custom_components.homeassistant_grenton.domain.api.clu_messages.action import (
    GrentonCluApiActionRequest,
)
from custom_components.homeassistant_grenton.dto.widgets.on_off import (
    GrentonWidgetOnOffDto,
)
from custom_components.homeassistant_grenton.dto.widgets.on_off_double import (
    GrentonWidgetOnOffDoubleDto,
)
from custom_components.homeassistant_grenton.mappers.device_mapper import DeviceMapper
from custom_components.homeassistant_grenton.options_flow import GrentonOptionsFlow


def widget(double=False):
    components = []
    for row in range(2 if double else 1):
        components.append(
            {
                "rowId": row,
                "label": f"Office {row}",
                "unit": "UNKNOWN",
                "onIndication": "ON",
                "offIndication": "OFF",
                "state": {
                    "callType": "ATTRIBUTE",
                    "cluId": "clu1",
                    "objectName": f"DOUT_{row}",
                    "index": "0",
                },
                "actions": [
                    {
                        "callType": "METHOD",
                        "event": event,
                        "cluId": "clu1",
                        "objectName": f"DOUT_{row}",
                        "index": "2",
                        "value": value,
                    }
                    for event, value in (("ON", "1"), ("OFF", "0"))
                ],
            }
        )
    cls = GrentonWidgetOnOffDoubleDto if double else GrentonWidgetOnOffDto
    return cls(id="office", components=components)


def coordinator(options=None):
    return SimpleNamespace(
        config_entry=SimpleNamespace(options=options or {}),
        clus=[
            SimpleNamespace(id="clu1", name="Main"),
            SimpleNamespace(id="clu2", name="Garage"),
        ],
        register_component_state=Mock(),
        get_value_for_component=Mock(return_value="ON"),
        execute_action=AsyncMock(),
        last_update_success=True,
        async_add_listener=Mock(return_value=Mock()),
    )


@pytest.mark.parametrize("entity_type", ["switch", "light"])
@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("ON", True),
        ("OFF", False),
        (1, True),
        (0, False),
        (True, True),
        (False, False),
    ],
)
def test_mapper_preserves_state_and_actions_for_both_domains(
    entity_type, value, expected
):
    coord = coordinator({"entities": {"office_0": {"entity_type": entity_type}}})
    entity = DeviceMapper.to_domain(widget(), coord).entities[0]
    assert isinstance(entity, LightEntity) == (entity_type == "light")
    assert isinstance(entity, SwitchEntity) == (entity_type == "switch")
    coord.get_value_for_component.return_value = value
    assert entity.is_on is expected
    coord.register_component_state.assert_called_once_with(entity.state_object)
    asyncio.run(entity.async_turn_on())
    asyncio.run(entity.async_turn_off())
    assert [
        GrentonCluApiActionRequest.from_action(call.args[0]).payload
        for call in coord.execute_action.await_args_list
    ] == ['DOUT_0:execute(2,"1")', 'DOUT_0:execute(2,"0")']
    if entity_type == "light":
        assert entity.supported_color_modes == {ColorMode.ONOFF}
        assert entity.brightness is None


def test_default_switch_and_independent_double_channels():
    assert isinstance(
        DeviceMapper.to_domain(widget(), coordinator()).entities[0], SwitchEntity
    )
    coord = coordinator({"entities": {"office_0": {"entity_type": "light"}}})
    device = DeviceMapper.to_domain(widget(double=True), coord)
    assert isinstance(device.entities[0], LightEntity)
    assert isinstance(device.entities[1], SwitchEntity)


def test_options_flow_validates_type_persists_it_and_schedules_reload(tmp_path):
    async def run():
        coord = coordinator({"other_option": True})
        device = DeviceMapper.to_domain(widget(), coord)
        entity = device.entities[0]
        entity.entity_id = "switch.office"
        entry = coord.config_entry
        entry.entry_id = "interface1"
        entry.update_listeners = []
        entry.runtime_data = SimpleNamespace(devices=[device])
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = SimpleNamespace(
            async_get_known_entry=Mock(return_value=entry),
            async_update_entry=Mock(return_value=True),
            async_schedule_reload=Mock(),
        )
        flow = GrentonOptionsFlow()
        flow.hass = hass
        flow.handler = entry.entry_id
        with patch.object(
            GrentonOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ):
            result = await flow.async_step_entity_list({"entity": entity.entity_id})
            assert result["step_id"] == "configure_on_off_state"
            fields = to_field_list(
                result["data_schema"], custom_serializer=cv.custom_serializer
            )
            assert fields[0]["default"] == "switch"
            result = await flow.async_step_configure_on_off_state(
                {"entity_type": "fan", "state": entity._config["state"]}
            )
            assert result["errors"] == {"entity_type": "invalid_configuration"}
            result = await flow.async_step_configure_on_off_state(
                {"entity_type": "light", "state": entity._config["state"]}
            )
            assert result["step_id"] == "configure_on_off_on_action"
            assert entity._config["entity_type"] == "switch"
            result = await flow.async_step_configure_on_off_on_action(
                {"action_on": entity._config["action_on"]}
            )
            assert result["step_id"] == "configure_on_off_off_action"
            result = await flow.async_step_configure_on_off_off_action(
                {"action_off": entity._config["action_off"]}
            )
            assert result["data"]["other_option"] is True
            assert result["data"]["entities"]["office_0"]["entity_type"] == "light"
            assert set(result["data"]["entities"]["office_0"]) == {
                "entity_type",
                "state",
                "action_on",
                "action_off",
            }
            await OptionsFlowManager(hass).async_finish_flow(flow, result)
        hass.config_entries.async_schedule_reload.assert_called_once_with(
            entry.entry_id
        )
        # The newly mapped light remains configurable, including switching back.
        coord.config_entry.options = result["data"]
        restored = DeviceMapper.to_domain(widget(), coord).entities[0]
        assert isinstance(restored, LightEntity)
        assert (
            restored._get_schema_instance().steps[0].step_id == "configure_on_off_state"
        )
        assert restored._config == result["data"]["entities"]["office_0"]

    asyncio.run(run())


def test_native_ha_domain_change_cleans_old_registration_and_keeps_device(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        entry = ConfigEntry(
            domain="grenton",
            title="Home",
            data={},
            options={},
            source="user",
            unique_id="interface1",
            version=1,
            minor_version=1,
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        hass.config_entries = SimpleNamespace(async_get_entry=lambda entry_id: entry)
        dr.async_setup(hass)
        await asyncio.gather(
            dr.async_load(hass, load_empty=True), er.async_load(hass, load_empty=True)
        )
        coord = coordinator()
        coord.config_entry = entry
        registry = er.async_get(hass)
        device_id = None
        previous_id = None
        for domain in ("switch", "light", "switch"):
            # ConfigEntry options are immutable; model the update used by HA.
            object.__setattr__(
                entry,
                "options",
                MappingProxyType({"entities": {"office_0": {"entity_type": domain}}}),
            )
            device = DeviceMapper.to_domain(widget(), coord)
            _cleanup_orphans(hass, entry, [device], [])
            if previous_id:
                assert registry.async_get(previous_id) is None
            platform = EntityPlatform(
                hass=hass,
                logger=logging.getLogger(__name__),
                domain=domain,
                platform_name="grenton",
                platform=None,
                scan_interval=timedelta(seconds=30),
                entity_namespace=None,
            )
            platform.config_entry = entry
            await platform.async_add_entities(device.entities)
            entity = device.entities[0]
            assert entity.entity_id.startswith(f"{domain}.")
            assert hass.states.get(entity.entity_id).state == "on"
            assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 1
            if device_id:
                assert entity.registry_entry.device_id == device_id
            device_id = entity.registry_entry.device_id
            if domain == "light":
                assert hass.states.get(entity.entity_id).attributes[
                    "supported_color_modes"
                ] == ["onoff"]
            previous_id = entity.entity_id
            await platform.async_reset()

    asyncio.run(run())


@pytest.mark.parametrize("language", ["en", "pl"])
def test_entity_type_dialog_has_translations(language):
    path = (
        Path(__file__).parents[1]
        / "custom_components/homeassistant_grenton/translations"
        / f"{language}.json"
    )
    data = json.loads(path.read_text())
    assert data["options"]["step"]["configure_on_off_state"]["data"]["entity_type"]
    assert set(data["selector"]["on_off_entity_types"]["options"]) == {
        "switch",
        "light",
    }


@pytest.mark.parametrize("entity_type", ["switch", "light"])
@pytest.mark.parametrize(
    "call_type, action, payload",
    [
        (
            "METHOD",
            {
                "object_name": "DOUT_2",
                "index": "3",
                "arguments": [{"type": "number", "value": 0}],
            },
            "DOUT_2:execute(3, 0)",
        ),
        (
            "SCRIPT",
            {
                "object_name": "Actions",
                "arguments": [
                    {"type": "string", "value": "lightOffice"},
                    {"type": "float", "value": -1.25},
                ],
            },
            'Actions("lightOffice", -1.25)',
        ),
        (
            "ATTRIBUTE",
            {"object_name": "DOUT_2", "index": "0", "value": "1"},
            'DOUT_2:set(0,"1")',
        ),
        (
            "VARIABLE",
            {"variable_name": "OfficeEnabled", "value": "0"},
            'setVar("OfficeEnabled","0")',
        ),
    ],
)
def test_saved_actions_execute_on_the_selected_clu_and_remain_inspectable(
    entity_type, call_type, action, payload
):
    async def run():
        coord = coordinator()
        entity = DeviceMapper.to_domain(widget(), coord).entities[0]
        imported_off = GrentonCluApiActionRequest.from_action(entity.action_off).payload
        options = await entity.apply_configuration(
            {
                "entity_type": entity_type,
                "state": {
                    "call_type": "VARIABLE",
                    "clu_id": "clu2",
                    "variable_name": "OfficeState",
                },
                "action_on": {"call_type": call_type, "clu_id": "clu2", **action},
            }
        )
        coord.config_entry.options = options
        coord.register_component_state.reset_mock()
        restored = DeviceMapper.to_domain(widget(double=True), coord)
        configured, untouched = restored.entities
        assert configured.state_object.clu_id == "clu2"
        assert configured.state_object.index == "OfficeState"
        assert configured.state_object.call_type == "VARIABLE"
        assert (
            coord.register_component_state.call_args_list[0].args[0]
            == configured.state_object
        )
        await configured.async_turn_on()
        await configured.async_turn_off()
        calls = coord.execute_action.await_args_list
        assert calls[0].args[0].clu_id == "clu2"
        assert (
            GrentonCluApiActionRequest.from_action(calls[0].args[0]).payload == payload
        )
        assert (
            GrentonCluApiActionRequest.from_action(calls[1].args[0]).payload
            == imported_off
        )
        assert configured.extra_state_attributes["action_on"]["call"] == payload
        assert (
            configured.extra_state_attributes["state_source"]["variable_name"]
            == "OfficeState"
        )
        assert untouched.state_object.object_name == "DOUT_1"
        assert (
            GrentonCluApiActionRequest.from_action(untouched.action_on).payload
            == 'DOUT_1:execute(2,"1")'
        )

    asyncio.run(run())


def test_three_forms_show_current_values_and_retain_invalid_argument_draft():
    async def run():
        coord = coordinator()
        device = DeviceMapper.to_domain(widget(), coord)
        entity = device.entities[0]
        entity.entity_id = "switch.office"
        coord.config_entry.runtime_data = SimpleNamespace(devices=[device])
        flow = GrentonOptionsFlow()
        with patch.object(
            GrentonOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=coord.config_entry,
        ):
            result = await flow.async_step_entity_list({"entity": entity.entity_id})

            def fields(form):
                return {
                    field["name"]: field
                    for field in to_field_list(
                        form["data_schema"], custom_serializer=cv.custom_serializer
                    )
                }

            state_field = fields(result)["state"]
            assert (
                state_field["description"]["suggested_value"] == entity._config["state"]
            )
            assert state_field["selector"]["grenton_scene"]["mode"] == "state"
            result = await flow.async_step_configure_on_off_state(
                {
                    "entity_type": "switch",
                    "state": {"call_type": "METHOD", "clu_id": "clu1"},
                }
            )
            assert result["errors"] == {"state": "invalid_configuration"}
            assert coord.config_entry.options == {}
            result = await flow.async_step_configure_on_off_state(
                {"entity_type": "switch", "state": entity._config["state"]}
            )
            action_field = fields(result)["action_on"]
            action_draft = action_field["description"]["suggested_value"]
            assert action_draft["value"] == "1"
            assert action_draft["arguments"] == [{"type": "number", "value": 1}]
            assert action_field["selector"]["grenton_scene"]["editable_value"] is True
            bad_action = {
                **action_draft,
                "arguments": [{"type": "number", "value": "abc"}],
            }
            result = await flow.async_step_configure_on_off_on_action(
                {"action_on": bad_action}
            )
            assert result["errors"] == {"action_on": "invalid_configuration"}
            assert (
                fields(result)["action_on"]["description"]["suggested_value"]
                == bad_action
            )
            result = await flow.async_step_configure_on_off_on_action(
                {"action_on": action_draft}
            )
            off_draft = fields(result)["action_off"]["description"]["suggested_value"]
            assert off_draft["arguments"] == [{"type": "number", "value": 0}]
            result = await flow.async_step_configure_on_off_off_action(
                {"action_off": {**off_draft, "arguments": []}}
            )
            assert result["type"] == "create_entry"
            assert (
                GrentonCluApiActionRequest.from_action(entity.action_on).payload
                == "DOUT_0:execute(2, 1)"
            )
            assert (
                GrentonCluApiActionRequest.from_action(entity.action_off).payload
                == "DOUT_0:execute(2)"
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "state_config, expected_payload",
    [
        (
            {
                "call_type": "ATTRIBUTE",
                "clu_id": "clu2",
                "object_name": "DOUT_2",
                "index": "9",
            },
            "SYSTEM:clientRegister(0,0,1,{{DOUT_2,9}})",
        ),
        (
            {
                "call_type": "VARIABLE",
                "clu_id": "clu2",
                "variable_name": 'office"state',
            },
            'SYSTEM:clientRegister(0,0,1,{"office\\"state"})',
        ),
    ],
)
def test_configured_state_uses_existing_subscription_protocol(
    state_config, expected_payload
):
    from custom_components.homeassistant_grenton.domain.api.clu_messages.client_register import (
        GrentonCluApiClientRegisterRequest,
    )
    from custom_components.homeassistant_grenton.state import (
        GrentonCluState,
        GrentonState,
    )

    coord = coordinator({"entities": {"office_0": {"state": state_config}}})
    state = GrentonState({"clu1": GrentonCluState(), "clu2": GrentonCluState()})
    coord.register_component_state.side_effect = state.register_state
    coord.get_value_for_component.side_effect = state.get_value_for_component
    entity = DeviceMapper.to_domain(widget(), coord).entities[0]
    keys = state.clus["clu2"].get_subscription_order()
    assert state.clus["clu1"].get_subscription_order() == []
    assert GrentonCluApiClientRegisterRequest(keys, 0).payload == expected_payload
    state.clus["clu2"].update_keys(keys, [1])
    assert entity.is_on is True
    state.clus["clu2"].update_keys(keys, [0])
    assert entity.is_on is False


def test_values_are_lua_escaped_for_editable_set_actions():
    from custom_components.homeassistant_grenton.domain.action_configuration import (
        configured_action,
    )
    from custom_components.homeassistant_grenton.domain.enums import (
        GrentonActionEventType,
    )

    action = configured_action(
        {
            "call_type": "VARIABLE",
            "clu_id": "clu1",
            "variable_name": "Office",
            "value": 'a"b\\c\n',
        },
        GrentonActionEventType.ON,
    )
    assert (
        GrentonCluApiActionRequest.from_action(action).payload
        == 'setVar("Office","a\\"b\\\\c\\n")'
    )


@pytest.mark.parametrize("entity_type", ["switch", "light"])
def test_on_off_imports_separate_method_arguments_in_both_action_forms(entity_type):
    async def run():
        coord = coordinator()
        dto = widget()
        dto.components[0].actions[0].value = "800,0"
        dto.components[0].actions[1].value = "0,800"
        device = DeviceMapper.to_domain(dto, coord)
        entity = device.entities[0]
        entity.entity_id = "switch.office"
        coord.config_entry.runtime_data = SimpleNamespace(devices=[device])
        flow = GrentonOptionsFlow()
        with patch.object(
            GrentonOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=coord.config_entry,
        ):
            await flow.async_step_entity_list({"entity": entity.entity_id})
            result = await flow.async_step_configure_on_off_state(
                {"entity_type": entity_type, "state": entity._config["state"]}
            )

            def draft(form, key):
                fields = {
                    field["name"]: field
                    for field in to_field_list(
                        form["data_schema"], custom_serializer=cv.custom_serializer
                    )
                }
                return fields[key]["description"]["suggested_value"]

            on_draft = draft(result, "action_on")
            assert on_draft["arguments"] == [
                {"type": "number", "value": 800},
                {"type": "number", "value": 0},
            ]
            result = await flow.async_step_configure_on_off_on_action(
                {"action_on": on_draft}
            )
            off_draft = draft(result, "action_off")
            assert off_draft["arguments"] == [
                {"type": "number", "value": 0},
                {"type": "number", "value": 800},
            ]
            result = await flow.async_step_configure_on_off_off_action(
                {"action_off": off_draft}
            )
            coord.config_entry.options = result["data"]
        restored = DeviceMapper.to_domain(dto, coord).entities[0]
        await restored.async_turn_on()
        await restored.async_turn_off()
        assert [
            GrentonCluApiActionRequest.from_action(call.args[0]).payload
            for call in coord.execute_action.await_args_list
        ] == ["DOUT_0:execute(2, 800, 0)", "DOUT_0:execute(2, 0, 800)"]
        assert (
            restored.extra_state_attributes["action_on"]["arguments"]
            == on_draft["arguments"]
        )
        assert (
            restored.extra_state_attributes["action_off"]["arguments"]
            == off_draft["arguments"]
        )

    asyncio.run(run())
