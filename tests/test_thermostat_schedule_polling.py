"""Large schedule reads are conditional, isolated and available on demand."""

import asyncio
from time import monotonic
from unittest.mock import AsyncMock

import pytest
from test_clu_scripts import make_entry, make_hass
from test_clu_state import coordinator, make_clu
from test_scene_configuration import make_flow, ui_fields
from test_thermostat_schedule import constant_week
from test_thermostat_v2 import widget

from custom_components.homeassistant_grenton.mappers.device_mapper import DeviceMapper
from custom_components.homeassistant_grenton.state import MISSING_STATE


async def fixture(tmp_path, *, poll_only=False, power=1, mode=0):
    entry = make_entry()
    hass = await make_hass(tmp_path, [entry])
    clu = make_clu("clu1", prefix="521" if poll_only else "221")
    coord = coordinator(hass, entry, [clu])
    entity = DeviceMapper.to_domain(widget(), coord).entities[0]
    values = {
        "6": power,
        "7": 0,
        "8": mode,
        "9": "Y0" * 672 + "+00:30",
        "10": 0,
        "11": 30,
        "12": 20,
        "13": 0,
        "14": 21,
    }
    api = coord._apis[clu.id]
    api.connect = AsyncMock(return_value=True)
    api.ping = AsyncMock(return_value=True)
    api.disconnect = AsyncMock()
    api.register_component_states = AsyncMock(
        side_effect=lambda keys, **kwargs: [values[key.name] for key in keys]
    )
    api.poll_component_states = AsyncMock(
        side_effect=lambda keys: [values[key.name] for key in keys]
    )

    async def write(action):
        values[action.index] = (
            action.value if action.index == "9" else float(action.value)
        )
        return True

    api.execute_action = AsyncMock(side_effect=write)
    return hass, coord, entity, api, values


def schedule_reads(api):
    return [
        call.args[0]
        for call in api.poll_component_states.await_args_list
        if any(key.name == "9" for key in call.args[0])
    ]


@pytest.mark.parametrize("poll_only", [False, True])
@pytest.mark.parametrize(
    "power, mode, expected",
    [
        (1, 2, True),
        (1, 0, False),
        (0, 2, False),
        (1, 1, False),
        (1, 3, False),
        (None, 2, False),
        (1, None, False),
    ],
)
def test_startup_reads_schedule_only_when_auto_is_active(
    tmp_path, poll_only, power, mode, expected
):
    async def run():
        hass, coord, entity, api, _ = await fixture(
            tmp_path, poll_only=poll_only, power=power, mode=mode
        )
        try:
            await coord.async_setup()
            key = coord._state_key(entity.states["scheduleData"])
            assert key not in coord._realtime_keys["clu1"]
            assert key in coord._slow_keys["clu1"]
            assert coord.state_read_mode(entity.states["scheduleData"]) == "slow"
            assert len(schedule_reads(api)) == int(expected)
            assert all(keys == [key] for keys in schedule_reads(api))
            assert (entity.decoded_schedule is not None) == expected
            assert all(
                key not in call.args[0]
                for call in api.register_component_states.await_args_list
            )
            assert not api._groups
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("poll_only", [False, True])
def test_mode_and_power_changes_start_and_stop_schedule_reads(tmp_path, poll_only):
    async def run():
        hass, coord, entity, api, values = await fixture(tmp_path, poll_only=poll_only)
        try:
            await coord.async_setup()
            assert not schedule_reads(api)
            for mode, power, expected in [
                (2, 1, True),
                (0, 1, False),
                (2, 0, False),
                (2, 1, True),
                (1, 1, False),
            ]:
                values.update({"8": mode, "6": power})
                if not poll_only:
                    await coord._process_report(
                        "clu1",
                        [
                            coord._state_key(entity.states[name])
                            for name in ("mode", "state")
                        ],
                        [mode, power],
                    )
                api.poll_component_states.reset_mock()
                await coord._send_slow_poll("clu1")
                assert bool(schedule_reads(api)) == expected
            # Generic refreshes must obey the same condition as timer polls.
            api.poll_component_states.reset_mock()
            assert await coord.async_refresh_clu_state("clu1")
            assert not schedule_reads(api)
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("poll_only", [False, True])
def test_manual_update_forces_one_verification_read_without_enabling_polling(
    tmp_path, poll_only
):
    async def run():
        hass, coord, entity, api, values = await fixture(tmp_path, poll_only=poll_only)
        try:
            await coord.async_setup()
            await entity.async_set_thermostat_schedule(constant_week(22))
            assert (
                len(schedule_reads(api)) == 2
            )  # Fresh encoding metadata, then one readback.
            assert set(entity.decoded_schedule.temperatures) == {22}
            assert values["8"] == 0 and values["6"] == 1
            api.execute_action.assert_awaited_once()
            api.poll_component_states.reset_mock()
            await coord._send_slow_poll("clu1")
            assert not schedule_reads(api)
            assert set(entity.decoded_schedule.temperatures) == {22}
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


def test_editor_loads_manual_schedule_once_and_keeps_it_while_polling_is_paused(
    tmp_path,
):
    async def run():
        hass, coord, entity, api, _ = await fixture(tmp_path)
        try:
            await coord.async_setup()
            assert entity.decoded_schedule is None
            entity.entity_id = "climate.living_room"
            flow, entry_patch = make_flow(coord, entity)
            with entry_patch:
                result = await flow.async_step_entity_list({"entity": entity.entity_id})
                assert result["step_id"] == "configure_thermostat"
                field = ui_fields(result)["thermostat"]
                assert field["selector"]["grenton_thermostat"]["schedule_available"]
                assert field["description"]["suggested_value"]["sections"]
                assert len(schedule_reads(api)) == 1
                await flow.async_step_configure_thermostat(
                    {"thermostat": {"cooling_enabled": False}}
                )
                assert len(schedule_reads(api)) == 1
            state = entity.states["scheduleData"]
            key = coord._state_key(state)
            _, stamp = coord._slow_success["clu1"][key]
            coord._slow_success["clu1"][key] = (monotonic() - 1000, stamp)
            assert coord.is_state_fresh(
                state
            )  # Retain the last confirmed snapshot while paused.
            assert entity.decoded_schedule is not None
            await coord._process_report(
                "clu1", [coord._state_key(entity.states["mode"])], [2]
            )
            assert not coord.is_state_fresh(
                state
            )  # Active schedules require current data.
            await coord._send_slow_poll("clu1")
            assert coord.is_state_fresh(state)
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


def test_failed_editor_read_shows_error_and_sends_no_writes(tmp_path):
    async def run():
        hass, coord, entity, api, _ = await fixture(tmp_path)
        try:
            await coord.async_setup()
            api.poll_component_states.side_effect = lambda keys: [
                MISSING_STATE for key in keys
            ]
            entity.entity_id = "climate.living_room"
            flow, entry_patch = make_flow(coord, entity)
            with entry_patch:
                result = await flow.async_step_entity_list({"entity": entity.entity_id})
            assert result["errors"] == {"base": "cannot_read_configuration"}
            assert not ui_fields(result)["thermostat"]["selector"][
                "grenton_thermostat"
            ]["schedule_available"]
            api.execute_action.assert_not_awaited()
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


def test_startup_waits_for_mode_on_another_polled_clu_before_reading_schedule(tmp_path):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        coord = coordinator(
            hass, entry, [make_clu("clu1"), make_clu("clu2", prefix="521")]
        )
        dto = widget()
        dto.object.mode.cluId = "clu2"
        dto.object.state.cluId = "clu2"
        entity = DeviceMapper.to_domain(dto, coord).entities[0]
        values = {
            "6": 1,
            "7": 0,
            "8": 2,
            "9": "Y0" * 672 + "+00:30",
            "10": 0,
            "11": 30,
            "12": 20,
            "13": 0,
            "14": 21,
        }

        async def read(keys):
            await asyncio.sleep(0)
            return [values[key.name] for key in keys]

        for api in coord._apis.values():
            api.connect = AsyncMock(return_value=True)
            api.ping = AsyncMock(return_value=True)
            api.disconnect = AsyncMock()
            api.register_component_states = AsyncMock(side_effect=read)
            api.poll_component_states = AsyncMock(side_effect=read)
        try:
            await coord.async_setup()
            assert entity.following_schedule is True
            assert entity.schedule_status == "ready"
            assert len(schedule_reads(coord._apis["clu1"])) == 1
            assert not schedule_reads(coord._apis["clu2"])
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())
