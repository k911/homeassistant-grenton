"""Device-scoped options flows, native registry links and admin access."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from homeassistant.config_entries import OptionsFlowManager
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from probatio import to_field_list
from test_clu_scripts import make_clu, make_entry
from test_on_off_configuration import coordinator, widget

from custom_components.homeassistant_grenton import async_setup_entry
from custom_components.homeassistant_grenton.device_configuration import (
    DEVICE_CONTEXT_KEY,
    async_open_device_configuration,
    configuration_url,
)
from custom_components.homeassistant_grenton.domain.entities.base import (
    BaseGrentonEntity,
)
from custom_components.homeassistant_grenton.domain.entities.clu import GrentonCluEntity
from custom_components.homeassistant_grenton.mappers.device_mapper import DeviceMapper
from custom_components.homeassistant_grenton.options_flow import GrentonOptionsFlow


def fields(result):
    return {
        field["name"]: field
        for field in to_field_list(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
    }


def test_device_scope_shows_only_its_entities_and_rejects_another_widget():
    async def run():
        coord = coordinator({"entities": {"other_0": {"entity_type": "light"}}})
        first = DeviceMapper.to_domain(widget(double=True), coord)
        other_dto = widget()
        other_dto.id = "other"
        second = DeviceMapper.to_domain(other_dto, coord)
        for device in (first, second):
            for entity in device.entities:
                entity.entity_id = "switch." + entity.unique_id
        coord.config_entry.runtime_data = SimpleNamespace(devices=[first, second])
        with patch.object(
            GrentonOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=coord.config_entry,
        ):
            global_flow = GrentonOptionsFlow()
            result = await global_flow.async_step_init()
            assert (
                len(fields(result)["entity"]["selector"]["entity"]["include_entities"])
                == 3
            )
            flow = GrentonOptionsFlow()
            flow.context = {DEVICE_CONTEXT_KEY: first.id}
            result = await flow.async_step_init()
            assert fields(result)["entity"]["selector"]["entity"][
                "include_entities"
            ] == ["switch.office_0", "switch.office_1"]
            result = await flow.async_step_entity_list({"entity": "switch.other_0"})
            assert result["reason"] == "entity_not_found"
            # A valid selection keeps the existing three ON_OFF forms.
            result = await flow.async_step_entity_list({"entity": "switch.office_1"})
            assert result["step_id"] == "configure_on_off_state"
            result = await flow.async_step_configure_on_off_state(
                {"entity_type": "switch", "state": first.entities[1]._config["state"]}
            )
            on_draft = fields(result)["action_on"]["description"]["suggested_value"]
            result = await flow.async_step_configure_on_off_on_action(
                {"action_on": on_draft}
            )
            off_draft = fields(result)["action_off"]["description"]["suggested_value"]
            result = await flow.async_step_configure_on_off_off_action(
                {"action_off": off_draft}
            )
            assert result["type"] == "create_entry"
            assert set(result["data"]["entities"]) == {"other_0", "office_1"}
            assert result["data"]["entities"]["other_0"] == {"entity_type": "light"}
            missing = GrentonOptionsFlow()
            missing.context = {DEVICE_CONTEXT_KEY: "removed"}
            assert (await missing.async_step_init())["reason"] == "device_not_found"

    asyncio.run(run())


def test_websocket_starts_scoped_native_options_flow_and_lists_all_widget_entities(
    tmp_path,
):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        entry = make_entry()
        coord = coordinator()
        coord.config_entry = entry
        device = DeviceMapper.to_domain(widget(double=True), coord)
        for entity in device.entities:
            entity.entity_id = "switch." + entity.unique_id
        read_only = BaseGrentonEntity(
            coord, "office_status", "Status", device.device_info
        )
        read_only.entity_id = "sensor.office_status"
        device.entities.append(read_only)
        entry.runtime_data = SimpleNamespace(devices=[device])
        manager = OptionsFlowManager(hass)
        hass.config_entries = SimpleNamespace(
            async_get_entry=lambda entry_id: (
                entry if entry_id == entry.entry_id else None
            ),
            async_get_known_entry=lambda entry_id: entry,
            options=manager,
        )
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        registered = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("grenton", device.id)},
            name="Office",
            configuration_url=configuration_url(entry.entry_id, device.id),
        )
        assert registered.configuration_url.startswith(
            "homeassistant://grenton-configure/"
        )
        connection = SimpleNamespace(send_result=Mock(), send_error=Mock())
        command = {
            "id": 1,
            "type": "grenton/device_configuration",
            "entry_id": entry.entry_id,
            "widget_id": device.id,
        }
        with patch.object(
            manager,
            "async_create_flow",
            AsyncMock(side_effect=lambda *args, **kwargs: GrentonOptionsFlow()),
        ):
            # Exercise the async handler; authorization is checked separately below.
            await async_open_device_configuration.__wrapped__.__wrapped__(
                hass, connection, command
            )
        connection.send_error.assert_not_called()
        result = connection.send_result.call_args.args[1]
        assert result["device_id"] == registered.id
        assert result["name"] == "Office"
        assert result["default_name"] == "Office"
        assert result["name_by_user"] is None
        assert result["widget_type"] == "ON_OFF_DOUBLE"
        assert [entity["configurable"] for entity in result["entities"]] == [
            True,
            True,
            False,
        ]
        assert result["flow"]["data_schema"][0]["selector"]["entity"][
            "include_entities"
        ] == ["switch.office_0", "switch.office_1"]
        flow = manager.async_get(result["flow"]["flow_id"])
        assert flow["context"][DEVICE_CONTEXT_KEY] == device.id
        manager.async_abort(flow["flow_id"])
        assert entry.options == {}
        dr.async_get(hass).async_update_device(registered.id, name_by_user="My office")
        with patch.object(manager, "async_create_flow", AsyncMock(side_effect=lambda *args, **kwargs: GrentonOptionsFlow())):
            await async_open_device_configuration.__wrapped__.__wrapped__(hass, connection, command)
        renamed = connection.send_result.call_args.args[1]
        assert renamed["name"] == "My office"
        assert renamed["name_by_user"] == "My office"
        assert renamed["default_name"] == "Office"
        manager.async_abort(renamed["flow"]["flow_id"])
        await async_open_device_configuration.__wrapped__.__wrapped__(
            hass, connection, {**command, "widget_id": "missing"}
        )
        assert connection.send_error.call_args.args[1] == "not_found"
        await async_open_device_configuration.__wrapped__.__wrapped__(
            hass, connection, {**command, "entry_id": "other-entry"}
        )
        assert connection.send_error.call_args.args[1] == "not_found"
        # Widgets without configurable entities can still show their inventory.
        device.entities = [read_only]
        await async_open_device_configuration.__wrapped__.__wrapped__(
            hass, connection, command
        )
        assert connection.send_result.call_args.args[1]["flow"] is None
        controller = GrentonCluEntity(coord, make_clu("clu1"))
        controller.entity_id = "sensor.main_controller"
        controller._attr_name = "Controller"
        entry.runtime_data.clu_entities = [controller]
        with patch.object(manager, "async_create_flow", AsyncMock(side_effect=lambda *args, **kwargs: GrentonOptionsFlow())):
            await async_open_device_configuration.__wrapped__.__wrapped__(
                hass, connection, {**command, "widget_id": controller.unique_id}
            )
        controller_result = connection.send_result.call_args.args[1]
        assert controller_result["widget_type"] == "CLU"
        assert controller_result["flow"]["step_id"] == "clu_variables"
        manager.async_abort(controller_result["flow"]["flow_id"])
        assert controller_result["entities"][0]["entity_id"] == "sensor.main_controller"
        entry.runtime_data = None
        await async_open_device_configuration.__wrapped__.__wrapped__(
            hass, connection, command
        )
        assert connection.send_error.call_args.args[1] == "not_found"

    asyncio.run(run())


def test_device_configuration_command_requires_admin():
    connection = SimpleNamespace(user=SimpleNamespace(is_admin=False))
    with pytest.raises(Unauthorized):
        async_open_device_configuration(None, connection, {"id": 1})


def test_widget_configuration_url_encodes_ids():
    assert (
        configuration_url("entry", "widget/with spaces")
        == "homeassistant://grenton-configure/entry/widget%2Fwith%20spaces"
    )


def test_setup_attaches_configuration_link_to_every_entity_in_a_widget(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        interface = {
            "id": "interface1",
            "version": 1,
            "name": "Home",
            "icon": "home",
            "theme": "GRENTON",
            "encryption": {"key": "00" * 16, "iv": "00" * 16},
            "clus": [
                {
                    "id": "clu1",
                    "serialNumber": "serial-clu1",
                    "name": "Main CLU",
                    "ip": "192.0.2.1",
                    "port": 1234,
                    "connectionType": "LOCAL_ONLY",
                }
            ],
            "pages": [],
            "pushNotifications": [],
        }
        entry = make_entry(interface=interface)
        coord = coordinator()
        coord.config_entry = entry
        coord.async_setup = AsyncMock()
        device = DeviceMapper.to_domain(widget(double=True), coord)
        hass.config_entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
        with (
            patch(
                "custom_components.homeassistant_grenton.GrentonCoordinator",
                return_value=coord,
            ),
            patch(
                "custom_components.homeassistant_grenton.DeviceMapper.from_mobile_interface",
                return_value=[device],
            ),
            patch("custom_components.homeassistant_grenton._cleanup_orphans"),
        ):
            await async_setup_entry(hass, entry)
        for entity in device.entities:
            assert entity.device_info["configuration_url"] == configuration_url(
                entry.entry_id, device.id
            )
        controller = entry.runtime_data.clu_entities[0]
        assert controller.device_info["configuration_url"] == configuration_url(
            entry.entry_id, controller.unique_id
        )

    asyncio.run(run())
