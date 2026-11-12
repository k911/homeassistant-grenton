"""Two-click slat measurements, native degree testing, and capability gating."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.config_entries import OptionsFlowManager
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from custom_components.homeassistant_grenton.domain.api.clu_messages.action import GrentonCluApiActionRequest
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
@pytest.mark.parametrize('initial_lamel_ms', [0, 3000])
def test_native_manager_two_click_measurement_then_degree_test_and_timeout_adjustment(tmp_path, direction, initial_lamel_ms):
    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, direction)
        values['10'] = initial_lamel_ms
        manager = OptionsFlowManager(hass)
        manager._async_add_flow_progress(flow)
        with entry_patch:
            await manager._async_handle_step(flow, 'entity_list', {'entity': entity.entity_id})
            await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_calibration'})
            configured_lamel_ms = initial_lamel_ms or 2500
            result = await manager.async_configure(flow.flow_id, {
                'direction': direction, 'tilt_enabled': True, 'lamel_move_timeout': configured_lamel_ms,
                f'limit_{direction}_ms': 45000})
            assert result['type'] == 'menu'
            assert flow._shutter_session.limit_ms == 45000
            session = flow._shutter_session
            session.POLL_SECONDS = .001
            result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_edge_ready'})
            assert result['type'] == 'progress'
            await flow._shutter_edge_task
            await asyncio.sleep(.01)
            result = await manager.async_configure(flow.flow_id)
            assert result['step_id'] == ('shutter_ready_tilt_up' if direction == 'up' else 'shutter_ready_tilt')
            clock = [100]
            with patch(CLOCK, side_effect=lambda: clock[0]):
                result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_start_measurement'})
                assert result['step_id'] == ('shutter_measurement_opening_tilt' if direction == 'up' else 'shutter_measurement_tilt_start')
                mark = 'tilt_finished' if direction == 'up' else 'tilt_started'
                assert result['data_schema']({}) == {'measurement': mark}
                before_mark = list(commands)
                clock[0] = 103.468 if direction == 'up' else 124.321
                result = await manager.async_configure(flow.flow_id, {'measurement': mark})
                assert result['step_id'] == ('shutter_measurement_opening_travel' if direction == 'up' else 'shutter_measurement_tilt_end')
                assert result['data_schema']({}) == {'measurement': 'finish'}
                assert session.phase_change_ms == (3468 if direction == 'up' else 24321)
                timer = ui_fields(result)['measurement']['selector']['grenton_shutter_measurement']
                assert timer['segment_started_ms'] == session.phase_change_ms
                assert timer['segment'] == ('travel' if direction == 'up' else 'tilt')
                assert commands == before_mark
                clock[0] = 127.789
                result = await manager.async_configure(flow.flow_id, {'measurement': 'finish'})
                assert result['step_id'] == 'shutter_tilt_review'
                assert session.elapsed_ms == 27789 and session.lamel_ms == 3468
                assert result['description_placeholders']['travel_ms'] == '24321'
                assert result['description_placeholders']['lamel_ms'] == '3468'
                assert result['data_schema']({}) == {
                    'timings': {'total_ms': 27900, 'travel_ms': 24400, 'lamel_move_timeout': 3500, 'keep_total': True, 'adapt_opposite': False}}
                measured = ui_fields(result)['timings']['selector']['grenton_shutter_timings']
                assert measured == {'direction': direction, 'measured_ms': 27789, 'measured_travel_ms': 24321,
                    'measured_lamel_ms': 3468, 'configured_up_ms': 22000, 'configured_down_ms': 21500,
                    'configured_lamel_ms': configured_lamel_ms}
                assert values['10'] == configured_lamel_ms  # The new measured time is not written until Save.
                result = await manager.async_configure(flow.flow_id,
                    {'timings': {'total_ms': 27500, 'travel_ms': 24000, 'lamel_move_timeout': 3500}})
                assert result['step_id'] == 'shutter_calibrated'
                index = '14' if direction == 'up' else '15'
                assert values[index] == 24000 and values['10'] == 3500
                assert 'ROLLER1:set(10,3500)' in commands
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
                assert result['data_schema']({}) == {'timings': {'total_ms': 27500, 'travel_ms': 24000, 'lamel_move_timeout': 3500, 'keep_total': True, 'adapt_opposite': False}}
                assert ui_fields(result)['timings']['selector']['grenton_shutter_timings'] == {
                    **measured, f'configured_{direction}_ms': 24000, 'configured_lamel_ms': 3500}
                result = await manager.async_configure(flow.flow_id,
                    {'timings': {'total_ms': 29000, 'travel_ms': 25000, 'lamel_move_timeout': 4000, 'keep_total': False, 'adapt_opposite': False}})
                assert result['step_id'] == 'shutter_calibrated'
                assert values[index] == 25000 and values['10'] == 4000
                result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_tilt_adjust'})
                assert result['data_schema']({}) == {'timings': {
                    'total_ms': 29000, 'travel_ms': 25000, 'lamel_move_timeout': 4000, 'keep_total': False, 'adapt_opposite': False}}
                assert ui_fields(result)['timings']['selector']['grenton_shutter_timings'] == {
                    **measured, f'configured_{direction}_ms': 25000, 'configured_lamel_ms': 4000}
                await manager.async_configure(flow.flow_id, result['data_schema']({}))
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
            result = await flow.async_step_shutter_calibration({'direction': 'down', 'limit_down_ms': 45000})
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
                assert result['description_placeholders']['configured_up_ms'] == '22000'
                assert result['description_placeholders']['configured_down_ms'] == '21500'
                assert set(ui_fields(result)) == {'total_ms'}
                result = await flow.async_step_shutter_review({'total_ms': 27321})
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
            assert session.elapsed_ms is None and session.phase_change_ms is None
            assert session.lamel_ms == 0
            await finish_motor(session, values)
            assert values['15'] == 21500 and values['10'] == 3000
        assert not any('execute(3' in command for command in commands)
    asyncio.run(run())


@pytest.mark.parametrize('direction', ['up', 'down'])
def test_tilt_measurement_requires_two_distinct_clicks_in_order(direction):
    async def run():
        entity, coord, values, commands = configured_shutter(tilt=3000, **{'7': 0 if direction == 'up' else 100})
        observe_motor(entity, coord, values)
        session = await entity.create_calibration(direction, 45000)
        session.POLL_SECONDS = .001
        await session.edge_ready()
        clock = [100]
        with patch(CLOCK, side_effect=lambda: clock[0]):
            await session.measure()
            before_mark = list(commands)
            mark = session.mark_tilt_finished if direction == 'up' else session.mark_tilt_started
            wrong_mark = session.mark_tilt_started if direction == 'up' else session.mark_tilt_finished
            with pytest.raises(HomeAssistantError):
                await wrong_mark()
            assert session.phase_change_ms is None and commands == before_mark
            with pytest.raises(HomeAssistantError):
                await session.stop_measurement()
            clock[0] = 103 if direction == 'up' else 124
            await mark()
            with pytest.raises(HomeAssistantError):
                await mark()
            with pytest.raises(HomeAssistantError):
                await session.stop_measurement()
            assert session.phase == ('travelling' if direction == 'up' else 'tilting')
            assert session.elapsed_ms is None
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
                if save:
                    result = await flow.async_step_shutter_review({
                        'timings': {'total_ms': 27000, 'travel_ms': 24000, 'lamel_move_timeout': 3000}})
                else:
                    result = await flow.async_step_shutter_cancel()
                assert session.running and values['0'] == 4
                if save:
                    assert result['step_id'] == 'shutter_calibrated'
                    result = await flow.async_step_shutter_done()
                    assert result['type'] == 'create_entry'
                    flow.async_remove()
                    assert session.phase == 'saved'
                else:
                    assert result['type'] == 'abort'
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
                    'timings': {'total_ms': 24000 + bad, 'travel_ms': 24000, 'lamel_move_timeout': bad}})
                assert result['errors']['base'] == 'cannot_apply_configuration'
            entity._config['tilt_enabled'] = False
            assert (await flow.async_step_shutter_calibrated())['menu_options'] == ['shutter_done']
            assert (await flow.async_step_shutter_tilt_test({'lamel_position': 45}))['type'] == 'abort'
            assert (await flow.async_step_shutter_tilt_adjust({
                'timings': {'total_ms': 27000, 'travel_ms': 24000, 'lamel_move_timeout': 3000}}))['type'] == 'abort'
            assert not commands
        await hass.async_stop()
    asyncio.run(run())


def test_opening_then_closing_update_one_shared_tilt_timeout_and_separate_travel_times(tmp_path):
    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, 'up')
        with entry_patch:
            for direction, boundary, total, travel, tilt in (
                ('up', 3001, 27322, 24321, 3001),
                ('down', 25321, 28789, 25321, 3468),
            ):
                session = await entity.create_calibration(direction, 45000)
                session.POLL_SECONDS = .001
                flow._shutter_session = session
                await session.edge_ready()
                clock = [100]
                with patch(CLOCK, side_effect=lambda: clock[0]):
                    await session.measure()
                    clock[0] = 100 + boundary / 1000
                    if direction == 'up':
                        await session.mark_tilt_finished()
                    else:
                        await session.mark_tilt_started()
                    clock[0] = 100 + total / 1000
                    await session.stop_measurement()
                    assert session.suggested_ms == travel and session.lamel_ms == tilt
                    result = await flow.async_step_shutter_review({
                        'timings': {'total_ms': travel + tilt, 'travel_ms': travel, 'lamel_move_timeout': tilt}})
                    assert result['step_id'] == 'shutter_calibrated'
                    await finish_motor(session, values)
                    assert values['14'] == 24321
                    assert values['15'] == (21500 if direction == 'up' else 25321)
                    assert values['10'] == tilt
                    assert entity._config == {'tilt_enabled': True}
            assert [command for command in commands if command.startswith('ROLLER1:set(10,')] == [
                'ROLLER1:set(10,3001)', 'ROLLER1:set(10,3468)']
            assert not any('execute(3' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('overrides', [
    {'total_ms': 27001}, {'total_ms': None}, {'total_ms': True},
    {'total_ms': 1200001}, {'total_ms': 27000.5},
    {'travel_ms': 0}, {'travel_ms': -1}, {'travel_ms': 600001},
    {'lamel_move_timeout': 0}, {'lamel_move_timeout': float('nan')},
    {'keep_total': 1}, {'measured_ms': 27000},
])
def test_invalid_or_inconsistent_timing_edits_never_write_to_the_clu(tmp_path, overrides):
    async def run():
        entity, _, _, commands, flow, entry_patch, hass = tilt_flow(tmp_path)
        with entry_patch:
            session = await entity.create_calibration('down', 45000)
            session.phase, session.finished = 'saved', True
            session.elapsed_ms, session.phase_change_ms, session.lamel_ms = 27000, 24000, 3000
            flow._shutter_session = session
            result = await flow.async_step_shutter_tilt_adjust({'timings': {
                'total_ms': 27000, 'travel_ms': 24000, 'lamel_move_timeout': 3000,
                'keep_total': False, **overrides}})
            assert result['errors']['base'] == 'cannot_apply_configuration'
            assert session.elapsed_ms == 27000 and session.suggested_ms == 24000 and session.lamel_ms == 3000
            assert not commands
        await hass.async_stop()
    asyncio.run(run())


def test_calibration_save_requires_attribute_10_readback_and_can_retry_without_a_checkbox(tmp_path):
    async def run():
        entity, coord, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path)
        values['10'] = 0
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
                result = await flow.async_step_shutter_stop_measurement()
                assert set(ui_fields(result)) == {'timings'}
                submitted = result['data_schema']({})
                original = coord.execute_action.side_effect
                async def ignore_timeout_write(action, **kwargs):
                    if action.index == '10' and hasattr(action, 'lua_value'):
                        commands.append(GrentonCluApiActionRequest.from_action(action).payload)
                        return
                    await original(action, **kwargs)
                coord.execute_action.side_effect = ignore_timeout_write
                result = await flow.async_step_shutter_review(submitted)
                assert result['errors']['base'] == 'cannot_apply_configuration'
                assert values['10'] == 0 and session.phase == 'review' and not session.finished
                assert commands[-1] == 'ROLLER1:set(10,3000)'
                assert any({source.index for source in call.kwargs['required_states']} == {'10', '15'}
                           for call in coord.async_refresh_clu_state.await_args_list)
                coord.execute_action.side_effect = original
                result = await flow.async_step_shutter_review(submitted)
                assert result['step_id'] == 'shutter_calibrated'
                assert values['10'] == 3000 and session.phase == 'saved'
                await finish_motor(session, values)
                assert values['10'] == 3000 and values['15'] == 24000
        assert not any('execute(3' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('direction', ['up', 'down'])
def test_direction_dialog_configures_tilt_support_and_timeout_without_moving(tmp_path, direction, enabled):
    from homeassistant.data_entry_flow import add_visible_conditions_to_serialized_schema, _strip_hidden_fields

    async def run():
        entity, coord, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, direction, enabled=not enabled)
        values['10'] = 3000 if not enabled else 0
        def update_entry(entry, *, options):
            entry.options = options
            return True
        hass.config_entries.async_update_entry.side_effect = update_entry
        manager = OptionsFlowManager(hass)
        manager._async_add_flow_progress(flow)
        with entry_patch:
            result = await manager._async_handle_step(flow, 'shutter_calibration', None)
            fields = list(ui_fields(result).values())
            add_visible_conditions_to_serialized_schema(result['data_schema'], fields)
            by_name = {field['name']: field for field in fields}
            assert set(by_name) == {'direction', 'limit_up_ms', 'limit_down_ms', 'tilt_enabled', 'lamel_move_timeout'}
            assert by_name['lamel_move_timeout']['visible'] == {'field': 'tilt_enabled', 'value': True}
            for selected, expected_time in (('up', 22000), ('down', 21500)):
                assert by_name[f'limit_{selected}_ms']['visible'] == {'field': 'direction', 'value': selected}
                active, _ = _strip_hidden_fields(result['data_schema'], {'direction': selected, 'tilt_enabled': enabled})
                defaults = active({'direction': selected, 'tilt_enabled': enabled})
                assert defaults[f'limit_{selected}_ms'] == expected_time
                assert ('limit_down_ms' if selected == 'up' else 'limit_up_ms') not in defaults
            hidden, _ = _strip_hidden_fields(result['data_schema'], {'tilt_enabled': False})
            assert 'lamel_move_timeout' not in {key.schema for key in hidden.schema}
            visible, _ = _strip_hidden_fields(result['data_schema'], {'tilt_enabled': True})
            assert 'lamel_move_timeout' in {key.schema for key in visible.schema}
            assert not commands
            result = await manager.async_configure(flow.flow_id, {
                'direction': direction, 'tilt_enabled': enabled,
                f'limit_{direction}_ms': 45000,
                'limit_down_ms' if direction == 'up' else 'limit_up_ms': 'stale invalid hidden value',
                'lamel_move_timeout': 3500 if enabled else 'stale invalid hidden value'})
            assert result['type'] == 'menu'
            assert result['step_id'] == ('shutter_position_closed' if direction == 'up' else 'shutter_position_open')
            assert values['10'] == (3500 if enabled else 0)
            assert entity._tilt_enabled is enabled
            assert coord.config_entry.options['entities'][entity.unique_id] == {'tilt_enabled': enabled}
            assert commands == [f'ROLLER1:set(10,{3500 if enabled else 0})']
            hass.config_entries.async_schedule_reload.assert_not_called()
            session = flow._shutter_session
            assert session.limit_ms == 45000
            assert session.tilt_enabled is enabled
            manager.async_abort(flow.flow_id)
            await asyncio.sleep(.01)
            assert session.finished and not session.running
            assert values['10'] == (3500 if enabled else 0)  # Save and continue committed the configuration.
            assert not any(':execute(' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('timeout', [0, -1, 600001, True, float('nan')])
def test_direction_dialog_rejects_invalid_tilt_timeout_before_writes(tmp_path, timeout):
    async def run():
        entity, _, _, commands, flow, entry_patch, hass = tilt_flow(tmp_path, enabled=False)
        with entry_patch:
            result = await flow.async_step_shutter_calibration({
                'direction': 'down', 'tilt_enabled': True, 'lamel_move_timeout': timeout})
            assert result['step_id'] == 'shutter_calibration'
            assert result['errors']['lamel_move_timeout'] == 'invalid_configuration'
            assert not commands and entity._tilt_enabled is False
            hass.config_entries.async_update_entry.assert_not_called()
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('direction', ['up', 'down'])
@pytest.mark.parametrize('limit', [0, 600001, None, 'invalid'])
def test_combined_preparation_validates_selected_movement_time_before_saving_tilt(tmp_path, direction, limit):
    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, direction, enabled=False)
        with entry_patch:
            result = await flow.async_step_shutter_calibration({
                'direction': direction, f'limit_{direction}_ms': limit,
                'tilt_enabled': True, 'lamel_move_timeout': 3500})
            assert result['step_id'] == 'shutter_calibration'
            assert result['errors'][f'limit_{direction}_ms'] == 'invalid_configuration'
            assert values['10'] == 3000 and entity._tilt_enabled is False
            assert entity._calibration is None and not commands
            hass.config_entries.async_update_entry.assert_not_called()
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('direction', ['up', 'down'])
def test_summary_reads_current_settings_and_excludes_provisional_calibration_limit(tmp_path, direction):
    async def run():
        entity, coord, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, direction)
        with entry_patch:
            session = await entity.create_calibration(direction, 45000)
            flow._shutter_session = session
            session.phase, session.elapsed_ms, session.lamel_ms = 'review', 27000, 3000
            session.phase_change_ms = 3000 if direction == 'up' else 24000
            session._temporary = True
            index, other_index = ('14', '15') if direction == 'up' else ('15', '14')
            values[index] = 45000
            async def fresh_read(*args, **kwargs):
                indices = {source.index for source in kwargs['required_states']}
                assert {'10', '14', '15'} <= indices
                values[other_index], values['10'] = 26789, 4500
                return True
            coord.async_refresh_clu_state.side_effect = fresh_read
            result = await flow.async_step_shutter_review()
            configured = ui_fields(result)['timings']['selector']['grenton_shutter_timings']
            assert configured[f'configured_{direction}_ms'] == session.original
            other = 'down' if direction == 'up' else 'up'
            assert configured[f'configured_{other}_ms'] == 26789
            assert configured['configured_lamel_ms'] == 4500
            assert configured['measured_travel_ms'] == 24000 and configured['measured_lamel_ms'] == 3000
            assert not commands
            coord.async_refresh_clu_state.side_effect = None
            await session.abort()
        await hass.async_stop()
    asyncio.run(run())


def test_summary_does_not_present_cached_settings_as_current_after_failed_read(tmp_path):
    async def run():
        entity, coord, _, commands, flow, entry_patch, hass = tilt_flow(tmp_path)
        with entry_patch:
            session = await entity.create_calibration('down', 45000)
            flow._shutter_session = session
            session.phase, session.elapsed_ms, session.phase_change_ms, session.lamel_ms = 'review', 27000, 24000, 3000
            coord.async_refresh_clu_state.return_value = False
            result = await flow.async_step_shutter_review()
            assert result['errors']['base'] == 'cannot_read_configuration'
            timings = ui_fields(result)['timings']['selector']['grenton_shutter_timings']
            assert all(timings[key] is None for key in ('configured_up_ms', 'configured_down_ms', 'configured_lamel_ms'))
            assert timings['measured_ms'] == 27000
            assert not commands
            await session.abort()
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('enabled,travel,lamel,rounded_travel,rounded_lamel', [
    (False, 27321, 0, 27400, 0),
    (False, 27400, 0, 27400, 0),
    (True, 24321, 3468, 24400, 3500),
    (True, 24400, 3500, 24400, 3500),
    (True, 1, 1, 100, 100),
    (True, 599999, 599999, 600000, 600000),
])
def test_summary_rounds_only_editable_defaults_up_to_100_ms(
    tmp_path, enabled, travel, lamel, rounded_travel, rounded_lamel,
):
    async def run():
        entity, _, values, _, flow, entry_patch, hass = tilt_flow(tmp_path, enabled=enabled)
        with entry_patch:
            session = await entity.create_calibration('down', 45000)
            flow._shutter_session = session
            session.phase, session.elapsed_ms, session.phase_change_ms, session.lamel_ms = (
                'review', travel + lamel, travel if enabled else None, lamel)
            result = await flow.async_step_shutter_review()
            submitted = result['data_schema']({})
            if enabled:
                assert submitted['timings'] == {
                    'total_ms': rounded_travel + rounded_lamel, 'travel_ms': rounded_travel,
                    'lamel_move_timeout': rounded_lamel, 'keep_total': True, 'adapt_opposite': False}
                reference = ui_fields(result)['timings']['selector']['grenton_shutter_timings']
                assert reference['measured_ms'] == travel + lamel
                assert reference['measured_travel_ms'] == travel
                assert reference['measured_lamel_ms'] == lamel
                assert reference['configured_down_ms'] == 21500
                assert reference['configured_lamel_ms'] == 3000
            else:
                assert submitted == {'total_ms': rounded_travel}
                assert result['description_placeholders']['measured_ms'] == str(travel)
            result = await flow.async_step_shutter_review(submitted)
            assert result['type'] == ('menu' if enabled else 'create_entry')
            assert values['15'] == rounded_travel
            if enabled:
                assert values['10'] == rounded_lamel
            assert session.elapsed_ms == travel + lamel and session.lamel_ms == lamel
        await hass.async_stop()
    asyncio.run(run())


def test_adjustment_dialog_keeps_existing_configured_precision(tmp_path):
    async def run():
        entity, _, values, _, flow, entry_patch, hass = tilt_flow(tmp_path)
        with entry_patch:
            session = await entity.create_calibration('down', 45000)
            flow._shutter_session = session
            session.phase, session.finished = 'saved', True
            session.elapsed_ms, session.phase_change_ms, session.lamel_ms = 27789, 24321, 3468
            values['15'], values['10'] = 24321, 3468
            result = await flow.async_step_shutter_tilt_adjust()
            assert result['data_schema']({})['timings'] == {
                'total_ms': 27789, 'travel_ms': 24321, 'lamel_move_timeout': 3468, 'keep_total': True, 'adapt_opposite': False}
        await hass.async_stop()
    asyncio.run(run())
