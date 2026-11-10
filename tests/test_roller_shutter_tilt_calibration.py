"""Two-click slat measurements, native degree testing, and capability gating."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.config_entries import OptionsFlowManager
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from test_scene_configuration import make_flow, ui_fields
from test_roller_shutter_configuration import (
    CLOCK, configured_shutter, finish_motor, observe_motor,
)


def tilt_flow(tmp_path, direction='down', enabled=True):
    entity, coord, values, commands = configured_shutter(
        tilt=3000, **{'7': 100 if direction == 'down' else 0})
    entity._config = {'tilt_enabled': enabled}
    observe_motor(entity, coord, values)
    flow, entry_patch = make_flow(coord, entity)
    hass = HomeAssistant(str(tmp_path))
    entry = coord.config_entry
    entry.entry_id, entry.update_listeners = 'interface1', []
    hass.config_entries = SimpleNamespace(async_get_known_entry=Mock(return_value=entry),
        async_update_entry=Mock(return_value=True), async_schedule_reload=Mock())
    flow.hass, flow.handler, flow.flow_id = hass, entry.entry_id, 'tilt-flow'
    flow.selected_entity_uid = entity.unique_id
    return entity, coord, values, commands, flow, entry_patch, hass


@pytest.mark.parametrize('direction', ['up', 'down'])
def test_native_manager_two_click_measurement_then_degree_test_and_timeout_adjustment(tmp_path, direction):
    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, direction)
        manager = OptionsFlowManager(hass)
        manager._async_add_flow_progress(flow)
        with entry_patch:
            await manager._async_handle_step(flow, 'entity_list', {'entity': entity.entity_id})
            await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_calibration'})
            result = await manager.async_configure(flow.flow_id, {'direction': direction})
            assert set(ui_fields(result)) == {f'limit_{direction}_ms'}
            await manager.async_configure(flow.flow_id, {f'limit_{direction}_ms': 45000})
            session = flow._shutter_session
            session.POLL_SECONDS = .001
            result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_edge_ready'})
            assert result['type'] == 'progress'
            await flow._shutter_edge_task
            await asyncio.sleep(.01)
            result = await manager.async_configure(flow.flow_id)
            assert result['step_id'] == 'shutter_ready_tilt'
            clock = [100]
            with patch(CLOCK, side_effect=lambda: clock[0]):
                result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_start_measurement'})
                assert result['step_id'] == 'shutter_measurement_tilt_start'
                assert result['data_schema']({}) == {'measurement': 'tilt_started'}
                before_mark = list(commands)
                clock[0] = 124.321
                result = await manager.async_configure(flow.flow_id, {'measurement': 'tilt_started'})
                assert result['step_id'] == 'shutter_measurement_tilt_end'
                assert result['data_schema']({}) == {'measurement': 'finish'}
                assert session.tilt_started_ms == 24321
                timer = ui_fields(result)['measurement']['selector']['grenton_shutter_measurement']
                assert timer['tilt_started_ms'] == 24321
                assert commands == before_mark
                clock[0] = 127.789
                result = await manager.async_configure(flow.flow_id, {'measurement': 'finish'})
                assert result['step_id'] == 'shutter_tilt_review'
                assert session.elapsed_ms == 27789 and session.lamel_ms == 3468
                assert result['description_placeholders']['travel_ms'] == '24321'
                assert result['description_placeholders']['lamel_ms'] == '3468'
                assert result['data_schema']({}) == {
                    'travel_ms': 24321, 'lamel_move_timeout': 3468, 'save': False}
                assert values['10'] == 3000  # No slat timing write before confirmation.
                result = await manager.async_configure(flow.flow_id,
                    {'travel_ms': 24000, 'lamel_move_timeout': 3500, 'save': True})
                assert result['step_id'] == 'shutter_calibrated'
                index = '14' if direction == 'up' else '15'
                assert values[index] == 24000 and values['10'] == 3500
                assert session.running and values['0'] == 4
                before_test = list(commands)
                result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_tilt_test'})
                assert result['type'] == 'progress' and commands == before_test
                wait_task = flow._shutter_wait_task
                await finish_motor(session, values)
                await wait_task
                await asyncio.sleep(.01)
                result = await manager.async_configure(flow.flow_id)
                assert result['step_id'] == 'shutter_tilt_test'
                angle = ui_fields(result)['lamel_position']['selector']['number']
                assert angle['min'] == 0 and angle['max'] == 90
                assert angle['unit_of_measurement'] == '°'
                for degrees in (0, 45, 90):
                    if degrees:
                        await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_tilt_test'})
                    result = await manager.async_configure(flow.flow_id, {'lamel_position': degrees})
                    assert result['step_id'] == 'shutter_calibrated'
                    assert commands[-1] == f'ROLLER1:execute(9,"{degrees}")'
                result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_tilt_adjust'})
                assert result['data_schema']({}) == {session.key: 24000, 'lamel_move_timeout': 3500}
                result = await manager.async_configure(flow.flow_id,
                    {session.key: 25000, 'lamel_move_timeout': 4000})
                assert result['step_id'] == 'shutter_calibrated'
                assert values[index] == 25000 and values['10'] == 4000
                result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_done'})
                assert result['type'] == 'create_entry'
                assert result['data']['entities'][entity.unique_id] == {'tilt_enabled': True}
                assert not any('execute(3' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())


def test_unchecked_tilt_support_uses_single_click_and_has_no_tilt_testing(tmp_path):
    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, enabled=False)
        with entry_patch:
            await flow.async_step_shutter_calibration({'direction': 'down'})
            result = await flow.async_step_shutter_limits({'limit_down_ms': 45000})
            session = flow._shutter_session
            session.POLL_SECONDS = .001
            assert not session.tilt_enabled
            await session.edge_ready()
            clock = [100]
            with patch(CLOCK, side_effect=lambda: clock[0]):
                assert (await flow.async_step_shutter_ready())['step_id'] == 'shutter_ready'
                result = await flow.async_step_shutter_start_measurement()
                assert result['step_id'] == 'shutter_measurement'
                assert result['data_schema']({}) == {'measurement': 'finish'}
                with pytest.raises(HomeAssistantError):
                    await session.mark_tilt_started()
                clock[0] = 127.321
                result = await flow.async_step_shutter_measurement({'measurement': 'finish'})
                assert result['step_id'] == 'shutter_review'
                assert set(ui_fields(result)) == {'total_ms', 'save'}
                result = await flow.async_step_shutter_review({'total_ms': 27321, 'save': True})
                assert result['type'] == 'create_entry'
                before = list(commands)
                assert (await flow.async_step_shutter_calibrated())['menu_options'] == ['shutter_done']
                for method in (flow.async_step_shutter_tilt_test, flow.async_step_shutter_tilt_adjust):
                    assert (await method())['type'] == 'abort'
                assert commands == before
                await finish_motor(session, values)
                assert values['10'] == 3000
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['measuring', 'tilting'])
def test_cancelling_either_stage_discards_both_times_and_never_stops_motor(stage):
    async def run():
        entity, coord, values, commands = configured_shutter(tilt=3000, **{'7': 100})
        observe_motor(entity, coord, values)
        session = await entity.create_calibration('down', 45000)
        session.POLL_SECONDS = .001
        await session.edge_ready()
        clock = [100]
        with patch(CLOCK, side_effect=lambda: clock[0]):
            await session.measure()
            clock[0] = 124
            if stage == 'tilting':
                await session.mark_tilt_started()
            await session.abort()
            assert session.running and values['0'] == 4
            assert session.elapsed_ms is None and session.tilt_started_ms is None
            assert session.lamel_ms == 0
            await finish_motor(session, values)
            assert values['15'] == 21500 and values['10'] == 3000
        assert not any('execute(3' in command for command in commands)
    asyncio.run(run())


def test_tilt_measurement_requires_two_distinct_clicks_in_order():
    async def run():
        entity, coord, values, _ = configured_shutter(tilt=3000, **{'7': 100})
        observe_motor(entity, coord, values)
        session = await entity.create_calibration('down', 45000)
        session.POLL_SECONDS = .001
        await session.edge_ready()
        clock = [100]
        with patch(CLOCK, side_effect=lambda: clock[0]):
            await session.measure()
            with pytest.raises(HomeAssistantError):
                await session.stop_measurement()
            clock[0] = 124
            await session.mark_tilt_started()
            with pytest.raises(HomeAssistantError):
                await session.mark_tilt_started()
            with pytest.raises(HomeAssistantError):
                await session.stop_measurement()
            assert session.phase == 'tilting' and session.elapsed_ms is None
            clock[0] = 127
            await session.stop_measurement()
            assert session.suggested_ms == 24000 and session.lamel_ms == 3000
            await session.abort()
            await finish_motor(session, values)
    asyncio.run(run())


@pytest.mark.parametrize('degrees', [-1, 91, 45.5, True, None, '45', float('nan'), float('inf')])
def test_invalid_degree_test_never_sends_a_command(degrees):
    entity, coord, _, _ = configured_shutter(tilt=3000)
    with pytest.raises(ServiceValidationError):
        asyncio.run(entity.async_set_lamel_position(degrees))
    coord.execute_action.assert_not_awaited()


@pytest.mark.parametrize('save', [False, True])
def test_review_can_be_discarded_or_saved_and_closed_while_motor_still_calibrates(tmp_path, save):
    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path)
        values['10'] = 0  # A measured timeout needs no manually supplied initial value.
        with entry_patch:
            session = await entity.create_calibration('down', 45000)
            session.POLL_SECONDS = .001
            flow._shutter_session = session
            await session.edge_ready()
            clock = [100]
            with patch(CLOCK, side_effect=lambda: clock[0]):
                await session.measure()
                clock[0] = 124
                await session.mark_tilt_started()
                clock[0] = 127
                await session.stop_measurement()
                result = await flow.async_step_shutter_review({
                    'travel_ms': 24000, 'lamel_move_timeout': 3000, 'save': save})
                assert session.running and values['0'] == 4
                if save:
                    assert result['step_id'] == 'shutter_calibrated'
                    result = await flow.async_step_shutter_done()
                    assert result['type'] == 'create_entry'
                    flow.async_remove()
                    assert session.phase == 'saved'
                else:
                    assert result['step_id'] == 'shutter'
                await finish_motor(session, values)
                assert values['15'] == (24000 if save else 21500)
                assert values['10'] == (3000 if save else 0)
                assert not any('execute(9' in command or 'execute(3' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())


def test_saved_tilt_tools_are_blocked_if_support_is_unchecked_and_invalid_times_do_not_write(tmp_path):
    async def run():
        entity, _, _, commands, flow, entry_patch, hass = tilt_flow(tmp_path)
        with entry_patch:
            session = await entity.create_calibration('down', 45000)
            session.phase, session.finished = 'saved', True
            flow._shutter_session = session
            for bad in (0, -1, 600001, True):
                result = await flow.async_step_shutter_tilt_adjust({
                    session.key: 24000, 'lamel_move_timeout': bad})
                assert result['errors']['base'] == 'cannot_apply_configuration'
            entity._config['tilt_enabled'] = False
            assert (await flow.async_step_shutter_calibrated())['menu_options'] == ['shutter_done']
            assert (await flow.async_step_shutter_tilt_test({'lamel_position': 45}))['type'] == 'abort'
            assert (await flow.async_step_shutter_tilt_adjust({
                session.key: 24000, 'lamel_move_timeout': 3000}))['type'] == 'abort'
            assert not commands
        await hass.async_stop()
    asyncio.run(run())
