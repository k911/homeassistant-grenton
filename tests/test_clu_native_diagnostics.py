"""Model-specific CLU diagnostics stay on slow polling and use confirmed values."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from test_clu_scripts import make_entry, make_hass
from test_clu_state import coordinator, make_clu

from custom_components.homeassistant_grenton.domain.api.clu_messages.action import (
    GrentonCluApiActionRequest,
)
from custom_components.homeassistant_grenton.domain.entities.clu_state import (
    GrentonCluDebugLogging,
    GrentonCluNativeState,
    GrentonCluResetReason,
    GrentonCluTelnetLogLevel,
    GrentonCluUptime,
    clu_entities,
)
from custom_components.homeassistant_grenton.state import MISSING_STATE

NATIVE_ENTITIES = (
    GrentonCluNativeState,
    GrentonCluTelnetLogLevel,
    GrentonCluResetReason,
    GrentonCluDebugLogging,
)

VALUES = [
    (GrentonCluNativeState, "221", "2", "native_value", 1, 3),
    (GrentonCluTelnetLogLevel, "221", "31", "native_value", 0, 4),
    (GrentonCluTelnetLogLevel, "521", "23", "native_value", 0, 4),
    (GrentonCluResetReason, "521", "26", "native_value", 0, 3),
    (GrentonCluDebugLogging, "221", "31", "is_on", 0, 4),
    (GrentonCluDebugLogging, "521", "23", "is_on", 0, 4),
]


@pytest.mark.parametrize("entity_class,prefix,index,property_name,initial,updated", VALUES)
def test_native_values_use_slow_polling_and_expire_failed_reads(
    tmp_path, entity_class, prefix, index, property_name, initial, updated
):
    async def run():
        clock = [0]
        with patch(
            "custom_components.homeassistant_grenton.coordinator.monotonic",
            side_effect=lambda: clock[0],
        ):
            entry = make_entry()
            hass = await make_hass(tmp_path, [entry])
            clu = make_clu("clu1", prefix=prefix)
            coord = coordinator(hass, entry, [clu])
            try:
                entity = entity_class(coord, clu)
                key = coord._state_key(entity.state_object)
                assert key.name == index
                assert coord.state_read_mode(entity.state_object) == "slow"
                assert key in coord._slow_keys[clu.id]
                assert key not in coord._realtime_keys[clu.id]
                api = coord._apis[clu.id]
                api.register_component_states = AsyncMock()
                api.poll_component_states = AsyncMock(return_value=[initial])
                assert await coord.async_refresh_clu_state(clu.id)
                before = getattr(entity, property_name)
                assert before is not None
                api.poll_component_states.assert_awaited_once_with([key])
                api.register_component_states.assert_not_awaited()

                # A missing reply retains the prior successful value temporarily.
                api.poll_component_states.return_value = [MISSING_STATE]
                clock[0] = 60
                await coord._send_slow_poll(clu.id)
                assert getattr(entity, property_name) == before
                clock[0] = 120
                await coord._send_slow_poll(clu.id)
                assert getattr(entity, property_name) is None
                assert coord.state.get_value_for_component(entity.state_object) == initial
                api.poll_component_states.return_value = [updated]
                clock[0] = 121
                await coord._send_slow_poll(clu.id)
                assert getattr(entity, property_name) is not None
                assert getattr(entity, property_name) != before
            finally:
                await coord.async_shutdown()
                await hass.async_stop()

    asyncio.run(run())


def test_discovery_limits_native_entities_to_supported_controller_families(tmp_path):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clus = [make_clu("clu1"), make_clu("clu2", prefix="521"), make_clu("other", prefix="999")]
        coord = coordinator(hass, entry, clus)
        try:
            actual = []
            for clu in clus:
                group = [entity for entity in clu_entities(coord, clu) if isinstance(entity, NATIVE_ENTITIES)]
                actual.append({type(entity) for entity in group})
                assert len({entity.unique_id for entity in group}) == len(group)
                for entity in group:
                    assert entity.device_info["identifiers"] == {("grenton", f"clu_{clu.id}")}
                    assert entity.entity_category == (
                        EntityCategory.CONFIG if isinstance(entity, GrentonCluDebugLogging)
                        else EntityCategory.DIAGNOSTIC
                    )
                if clu.serial_number.startswith("221"):
                    # Internal Log (index 1) is deliberately excluded from HA.
                    registered = coord.state.clus[clu.id].get_subscription_order()
                    assert all(key.name != "1" for key in registered)
                    api = coord._apis[clu.id]
                    api.register_component_states = AsyncMock()
                    api.poll_component_states = AsyncMock(
                        side_effect=lambda requested: [MISSING_STATE] * len(requested)
                    )
                    await coord.async_refresh_clu_state(clu.id)
                    api.poll_component_states.assert_awaited()
                    api.register_component_states.assert_not_awaited()
                    assert all(
                        key.name != "1"
                        for call in api.poll_component_states.await_args_list
                        for key in call.args[0]
                    )
            assert actual == [
                {GrentonCluNativeState, GrentonCluTelnetLogLevel, GrentonCluDebugLogging},
                {GrentonCluResetReason, GrentonCluTelnetLogLevel, GrentonCluDebugLogging},
                set(),
            ]
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "entity_class,prefix,cases",
    [
        (GrentonCluNativeState, "221", {
            0: "system_starting", 1: "system_ok", 2: "telnet_logging", 3: "emergency",
            4: "critical_error", 5: "monitor", 6: "module_not_responding",
            7: "zwave_inclusion", 8: "zwave_exclusion", 9: "zwave_busy",
            10: "zwave_error", 11: "zwave_success",
        }),
        (GrentonCluTelnetLogLevel, "221", {0: "off", 1: "error", 2: "warning", 3: "info", 4: "debug"}),
        (GrentonCluResetReason, "521", {0: "power_on", 1: "external_reset", 2: "configuration_reload", 3: "system_exception"}),
    ],
)
def test_enum_values_expose_raw_codes_and_keep_unknown_values_unknown(
    tmp_path, entity_class, prefix, cases
):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clu = make_clu("clu1", prefix=prefix)
        coord = coordinator(hass, entry, [clu])
        try:
            entity = entity_class(coord, clu)
            assert entity.device_class == SensorDeviceClass.ENUM
            assert set(entity.options) == set(cases.values())
            api = coord._apis[clu.id]
            api.poll_component_states = AsyncMock()
            for raw, expected in cases.items():
                api.poll_component_states.return_value = [raw]
                await coord._send_slow_poll(clu.id)
                assert entity.native_value == expected
                assert entity.extra_state_attributes["raw_value"] == raw
            for raw in (99, -1, "unrecognized", 1.5, True):
                api.poll_component_states.return_value = [raw]
                await coord._send_slow_poll(clu.id)
                assert entity.native_value is None
                assert entity.extra_state_attributes["raw_value"] == raw
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


def test_debug_switch_distinguishes_non_debug_levels_from_invalid_values(tmp_path):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clu = make_clu("clu1")
        coord = coordinator(hass, entry, [clu])
        try:
            switch = GrentonCluDebugLogging(coord, clu)
            api = coord._apis[clu.id]
            api.poll_component_states = AsyncMock()
            for raw, expected in (
                (0, False), (1, False), (2, False), (3, False), (4, True),
                ("4", True), (None, None), (True, None), (99, None),
                (-1, None), ("not a level", None),
            ):
                api.poll_component_states.return_value = [raw]
                await coord._send_slow_poll(clu.id)
                assert switch.is_on is expected
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("prefix,index", [("221", "31"), ("521", "23")])
def test_debug_switch_writes_numeric_level_and_uses_targeted_confirmed_readback(tmp_path, prefix, index):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clu = make_clu("clu1", prefix=prefix)
        coord = coordinator(hass, entry, [clu])
        try:
            unrelated = GrentonCluUptime(coord, clu)
            level = GrentonCluTelnetLogLevel(coord, clu)
            switch = GrentonCluDebugLogging(coord, clu)
            assert level.state_object == switch.state_object
            key = coord._state_key(switch.state_object)
            api = coord._apis[clu.id]
            api.execute_action = AsyncMock(return_value=True)
            api.register_component_states = AsyncMock()
            api.poll_component_states = AsyncMock(return_value=[2])
            await coord.async_refresh_clu_state(clu.id, required_states=[switch.state_object])
            assert switch.is_on is False
            api.poll_component_states.reset_mock()

            # An acknowledged write alone must not invent a new current level.
            await switch.async_turn_on()
            action = api.execute_action.await_args.args[0]
            assert GrentonCluApiActionRequest.from_action(action).payload == f"{clu.id}:set({index},4)"
            assert action.clu_id == clu.id
            api.poll_component_states.assert_awaited_once_with([key])
            assert switch.is_on is False
            assert level.native_value == "warning"
            assert coord.last_polled(unrelated.state_object) is None

            api.poll_component_states.return_value = [4]
            await switch.async_turn_on()
            assert switch.is_on is True
            assert level.native_value == "debug"
            api.poll_component_states.return_value = [0]
            await switch.async_turn_off()
            assert GrentonCluApiActionRequest.from_action(api.execute_action.await_args.args[0]).payload == f"{clu.id}:set({index},0)"
            assert switch.is_on is False
            assert level.native_value == "off"

            api.execute_action.return_value = False
            api.poll_component_states.reset_mock()
            with pytest.raises(HomeAssistantError, match="rejected"):
                await switch.async_turn_on()
            api.poll_component_states.assert_not_awaited()
            assert switch.is_on is False
            api.register_component_states.assert_not_awaited()
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())
