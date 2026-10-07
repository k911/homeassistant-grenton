"""CLU variable subscriptions, native HA entities and cloud control."""

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from test_clu_scripts import make_clu, make_entry, make_hass

from custom_components.homeassistant_grenton import _cleanup_orphans, async_setup
from custom_components.homeassistant_grenton.binary_sensor import (
    async_setup_entry as setup_binary_sensors,
)
from custom_components.homeassistant_grenton.coordinator import GrentonCoordinator
from custom_components.homeassistant_grenton.device_configuration import (
    async_open_device_configuration,
    configuration_url,
)
from custom_components.homeassistant_grenton.domain.api.clu import (
    GrentonCluApiProtocol,
    _SubscriptionEndpoint,
)
from custom_components.homeassistant_grenton.domain.api.clu_messages.action import (
    GrentonCluApiActionRequest,
)
from custom_components.homeassistant_grenton.domain.api.clu_messages.client_register import (
    GrentonCluApiClientRegisterRequest,
    GrentonCluApiClientRegisterResponse,
)
from custom_components.homeassistant_grenton.domain.encryption import GrentonEncryption
from custom_components.homeassistant_grenton.domain.entities.clu_state import (
    GrentonCluCloudConnection,
    GrentonCluFirmwareVersion,
    GrentonCluUptime,
    GrentonCluUseCloud,
    clu_entities,
)
from custom_components.homeassistant_grenton.integration_config import RuntimeData
from custom_components.homeassistant_grenton.sensor import (
    async_setup_entry as setup_sensors,
)
from custom_components.homeassistant_grenton.switch import (
    async_setup_entry as setup_switches,
)


def coordinator(hass, entry, clus):
    return GrentonCoordinator(
        hass,
        entry,
        clus,
        GrentonEncryption("AAAAAAAAAAAAAAAAAAAAAA==", "AAAAAAAAAAAAAAAAAAAAAA=="),
    )


async def register_platforms(hass, entry):
    translations = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components/homeassistant_grenton/translations/en.json"
        ).read_text()
    )["entity"]
    platforms = []
    for domain, setup in [
        ("sensor", setup_sensors),
        ("binary_sensor", setup_binary_sensors),
        ("switch", setup_switches),
    ]:
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
        names = {
            f"component.grenton.entity.{domain}.{key}.name": value["name"]
            for key, value in translations[domain].items()
            if "name" in value
        }
        platform.platform_data.platform_translations = names
        platform.platform_data.object_id_platform_translations = names
        platform.platform_data.default_language_platform_translations = names
        collected = Mock()
        await setup(hass, entry, collected)
        await platform.async_add_entities(collected.call_args.args[0])
        platforms.append(platform)
    return platforms


def test_all_four_variables_register_on_every_clu_and_update_native_entities(tmp_path):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clus = [make_clu("clu1"), make_clu("clu2", "Second CLU")]
        coord = coordinator(hass, entry, clus)
        entities = [entity for clu in clus for entity in clu_entities(coord, clu)]
        entry.runtime_data = RuntimeData(coord, [], entities)
        for entity in entities:
            entity.device_info["configuration_url"] = configuration_url(
                entry.entry_id, f"clu_{entity.clu.id}"
            )
        for clu in clus:
            keys = coord.state.clus[clu.id].get_subscription_order()
            assert [key.name for key in keys] == [
                "Uptime",
                "CloudConnection",
                "UseCloud",
                "FirmwareVersion",
            ]
            assert (
                GrentonCluApiClientRegisterRequest(keys, 0).payload
                == 'SYSTEM:clientRegister(0,0,1,{"Uptime","CloudConnection","UseCloud","FirmwareVersion"})'
            )
            await coord._process_report(clu.id, keys, [123, True, False, "3.1.0"])
        platforms = await register_platforms(hass, entry)
        assert [len(platform.entities) for platform in platforms] == [6, 2, 2]
        registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
        assert len(device_registry.devices) == 2
        for clu in clus:
            group = [entity for entity in entities if entity.clu.id == clu.id]
            device_ids = {
                registry.async_get(entity.entity_id).device_id for entity in group
            }
            assert len(device_ids) == 1
            controller, uptime, cloud, use_cloud, firmware = group
            device = device_registry.async_get(next(iter(device_ids)))
            assert device.sw_version == "3.1.0"
            assert hass.states.get(controller.entity_id).state == clu.serial_number
            assert hass.states.get(uptime.entity_id).state == "123"
            assert uptime.device_class == SensorDeviceClass.DURATION
            assert uptime.native_unit_of_measurement == "s"
            assert hass.states.get(cloud.entity_id).state == "on"
            assert cloud.device_class == BinarySensorDeviceClass.CONNECTIVITY
            assert hass.states.get(use_cloud.entity_id).state == "off"
            assert use_cloud.entity_category == EntityCategory.CONFIG
            assert all(
                entity.entity_category == EntityCategory.DIAGNOSTIC
                for entity in [uptime, cloud, firmware]
            )
            assert hass.states.get(firmware.entity_id).state == "3.1.0"

        # A report changes only its own CLU, including a reboot and new firmware.
        keys = coord.state.clus["clu1"].get_subscription_order()
        await coord._process_report("clu1", keys, [0, False, True, "3.2.0"])
        assert entities[1].native_value == 0
        assert entities[2].is_on is False
        assert entities[3].is_on is True
        assert entities[6].native_value == 123
        first_device_id = entities[0].registry_entry.device_id
        assert device_registry.async_get(first_device_id).sw_version == "3.2.0"

        # The device popup includes all five entities and identifies UseCloud
        # as a control, while the existing run_script device target runs once.
        connection = SimpleNamespace(send_result=Mock(), send_error=Mock())
        await async_open_device_configuration.__wrapped__.__wrapped__(
            hass,
            connection,
            {
                "id": 1,
                "type": "grenton/device_configuration",
                "entry_id": entry.entry_id,
                "widget_id": "clu_clu1",
            },
        )
        inventory = connection.send_result.call_args.args[1]
        assert len(inventory["entities"]) == 5
        assert (
            sum(entity["configuration_control"] for entity in inventory["entities"])
            == 1
        )
        assert inventory["flow"] is None
        await async_setup(hass, {})
        coord.execute_action = AsyncMock()
        await hass.services.async_call(
            "grenton",
            "run_script",
            {"script": "Actions"},
            target={"device_id": first_device_id},
            blocking=True,
        )
        coord.execute_action.assert_awaited_once()
        assert coord.execute_action.await_args.args[0].clu_id == "clu1"

        # Reload/reconfigure preserves all surviving identities and removes
        # diagnostics and configuration entities belonging to a removed CLU.
        kept = {entity.entity_id for entity in entities[:5]}
        removed = {entity.entity_id for entity in entities[5:]}
        removed_device_id = entities[5].registry_entry.device_id
        for platform in platforms:
            await platform.async_reset()
        replacement = clu_entities(coord, clus[0])
        _cleanup_orphans(hass, entry, [], replacement)
        assert all(registry.async_get(entity_id) for entity_id in kept)
        assert all(registry.async_get(entity_id) is None for entity_id in removed)
        assert device_registry.async_get(removed_device_id) is None
        assert device_registry.async_get(first_device_id).sw_version == "3.2.0"
        entry.runtime_data.clu_entities = replacement
        reloaded = await register_platforms(hass, entry)
        assert {entity.entity_id for entity in replacement} == kept
        for platform in reloaded:
            await platform.async_reset()

    asyncio.run(run())


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("false", False),
        ("1", True),
        ("0", False),
        (None, None),
        ("", None),
        ("invalid", None),
    ],
)
def test_boolean_variables_preserve_false_and_unknown_values(tmp_path, value, expected):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clu = make_clu("clu1")
        coord = coordinator(hass, entry, [clu])
        for cls in [GrentonCluCloudConnection, GrentonCluUseCloud]:
            entity = cls(coord, clu)
            assert entity.is_on is None
            keys = coord.state.clus[clu.id].get_subscription_order()
            coord.state.clus[clu.id].update_keys(keys, [value] * len(keys))
            assert entity.is_on is expected

    asyncio.run(run())


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0),
        (123, 123),
        ("123", 123),
        (123.0, 123),
        (1.5, None),
        (-1, None),
        (True, None),
        (None, None),
        ("", None),
        ("invalid", None),
    ],
)
def test_uptime_is_a_nonnegative_integer_or_unknown(tmp_path, value, expected):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clu = make_clu("clu1")
        coord = coordinator(hass, entry, [clu])
        entity = GrentonCluUptime(coord, clu)
        keys = coord.state.clus[clu.id].get_subscription_order()
        coord.state.clus[clu.id].update_keys(keys, [value])
        assert entity.native_value == expected

    asyncio.run(run())


def test_use_cloud_writes_boolean_to_correct_clu_and_reads_back_without_assuming_success(
    tmp_path,
):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clu = make_clu("clu2")
        coord = coordinator(hass, entry, [clu])
        entity = GrentonCluUseCloud(coord, clu)
        keys = coord.state.clus[clu.id].get_subscription_order()
        coord.state.clus[clu.id].update_keys(keys, [False])
        api = SimpleNamespace(
            execute_action=AsyncMock(return_value=True),
            register_component_states=AsyncMock(return_value=[False]),
        )
        coord._apis[clu.id] = api
        await entity.async_turn_on()
        action = api.execute_action.await_args.args[0]
        assert action.clu_id == "clu2"
        assert (
            GrentonCluApiActionRequest.from_action(action).payload
            == 'setVar("UseCloud",true)'
        )
        api.register_component_states.assert_awaited_once_with(keys)
        assert entity.is_on is False, (
            "a successful request must not invent a changed CLU setting"
        )
        api.register_component_states.return_value = [True]
        await entity.async_turn_on()
        assert entity.is_on is True
        api.register_component_states.return_value = [False]
        await entity.async_turn_off()
        assert (
            GrentonCluApiActionRequest.from_action(
                api.execute_action.await_args.args[0]
            ).payload
            == 'setVar("UseCloud",false)'
        )
        assert entity.is_on is False
        api.execute_action.return_value = False
        api.register_component_states.reset_mock()
        with pytest.raises(HomeAssistantError, match="rejected"):
            await entity.async_turn_on()
        api.register_component_states.assert_not_awaited()
        assert entity.is_on is False

    asyncio.run(run())


def test_firmware_strings_survive_initial_response_and_pushed_notifications(tmp_path):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clu = make_clu("clu1")
        coord = coordinator(hass, entry, [clu])
        firmware = GrentonCluFirmwareVersion(coord, clu)
        keys = coord.state.clus[clu.id].get_subscription_order()
        api = coord._apis[clu.id]
        response = 'resp:192.0.2.1:abcd:clientReport:0:{"1.20"}'
        endpoint = _SubscriptionEndpoint(
            None,
            SimpleNamespace(send_request=AsyncMock(return_value=response)),
            keys,
            0,
        )
        values = await api._register_chunk(endpoint)
        assert values == ["1.20"]
        await coord._process_report(clu.id, keys, values)
        assert firmware.native_value == "1.20"
        assert firmware.device_info["sw_version"] == "1.20"
        protocol = GrentonCluApiProtocol(api)
        protocol.subscription_keys = keys

        async def handle_report(values):
            await coord._process_report(clu.id, keys, values)

        protocol.subscription_callback = AsyncMock(side_effect=handle_report)
        protocol._process_response('resp:192.0.2.1:00000000:clientReport:0:{"00123"}')
        await asyncio.sleep(0)
        assert firmware.native_value == "00123"
        # Unrelated numeric/boolean variables keep their existing decoding.
        assert GrentonCluApiClientRegisterResponse(
            "resp:192.0.2.1:abcd:clientReport:0:{123,false}"
        ).values == [123, False]

    asyncio.run(run())
