"""CLU diagnostics use slow reads without driving whole-group realtime reports."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from test_clu_scripts import make_entry, make_hass
from test_clu_state import coordinator, make_clu
from test_clu_variables import set_options

from custom_components.homeassistant_grenton.domain.entities.clu_state import (
    GrentonCluBusVoltage,
    GrentonCluUptime,
)
from custom_components.homeassistant_grenton.domain.state_object import (
    GrentonAttributeValueObject,
)
from custom_components.homeassistant_grenton.state import MISSING_STATE


@pytest.mark.parametrize(
    "entity_class,initial",
    [(GrentonCluUptime, 3600), (GrentonCluBusVoltage, 24.5)],
    ids=["uptime", "bus_voltage"],
)
def test_diagnostic_uses_each_clus_slow_interval_and_accepts_zero(
    tmp_path, entity_class, initial
):
    async def run():
        entry = make_entry()
        set_options(entry, {"clu_read_settings": {"clu2": {"slow_read_interval": 10}}})
        hass = await make_hass(tmp_path, [entry])
        clus = [make_clu("clu1"), make_clu("clu2")]
        coord = coordinator(hass, entry, clus)
        entities = [entity_class(coord, clu) for clu in clus]
        for entity in entities:
            clu_id = entity.clu.id
            api = coord._apis[clu_id]
            key = coord._state_key(entity.state_object)
            assert key not in coord._realtime_keys[clu_id]
            assert key in coord._slow_keys[clu_id]
            assert coord.state_read_mode(entity.state_object) == "slow"
            api.register_component_states = AsyncMock()
            api.poll_component_states = AsyncMock(return_value=[initial])
            assert await coord.async_refresh_clu_state(clu_id)
            api.register_component_states.assert_not_awaited()
            api.poll_component_states.assert_awaited_once_with([key])
            assert entity.native_value == initial
            api.poll_component_states.return_value = [0]
            await coord._send_slow_poll(clu_id)
            assert entity.native_value == 0
        assert coord._slow_read_intervals == {"clu1": 60, "clu2": 10}
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("realtime_first", [False, True])
@pytest.mark.parametrize(
    "entity_class,index,value",
    [(GrentonCluUptime, "0", 123), (GrentonCluBusVoltage, "27", 24.5)],
    ids=["uptime", "bus_voltage"],
)
def test_explicit_realtime_consumer_of_diagnostic_keeps_existing_precedence(
    tmp_path, realtime_first, entity_class, index, value
):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        clu = make_clu("clu1")
        coord = coordinator(hass, entry, [clu])
        shared = GrentonAttributeValueObject(clu.id, clu.id, index)
        if realtime_first:
            coord.register_component_state(shared)
        entity = entity_class(coord, clu)
        if not realtime_first:
            coord.register_component_state(shared)
        api = coord._apis[clu.id]
        api.register_component_states = AsyncMock(return_value=[value])
        api.poll_component_states = AsyncMock()
        assert await coord.async_refresh_clu_state(clu.id)
        assert coord.state_read_mode(entity.state_object) == "realtime"
        api.register_component_states.assert_awaited_once_with(
            [coord._state_key(shared)]
        )
        api.poll_component_states.assert_not_awaited()
        assert entity.native_value == value
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "entity_class,initial,recovered",
    [(GrentonCluUptime, 3600, 3721), (GrentonCluBusVoltage, 24.5, 24.8)],
    ids=["uptime", "bus_voltage"],
)
def test_failed_diagnostic_poll_retains_cache_but_expires_visible_value(
    tmp_path, entity_class, initial, recovered
):
    async def run():
        clock = [0]
        with patch(
            "custom_components.homeassistant_grenton.coordinator.monotonic",
            side_effect=lambda: clock[0],
        ):
            entry = make_entry()
            hass = await make_hass(tmp_path, [entry])
            clu = make_clu("clu1")
            coord = coordinator(hass, entry, [clu])
            entity = entity_class(coord, clu)
            api = coord._apis[clu.id]
            api.poll_component_states = AsyncMock(return_value=[initial])
            await coord._send_slow_poll(clu.id)
            api.poll_component_states.return_value = [MISSING_STATE]
            clock[0] = 60
            await coord._send_slow_poll(clu.id)
            assert entity.native_value == initial
            clock[0] = 120
            await coord._send_slow_poll(clu.id)
            assert entity.native_value is None
            assert coord.state.get_value_for_component(entity.state_object) == initial
            api.poll_component_states.return_value = [recovered]
            clock[0] = 121
            await coord._send_slow_poll(clu.id)
            assert entity.native_value == recovered
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())
