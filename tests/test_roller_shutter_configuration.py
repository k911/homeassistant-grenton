"""Native shutter writes and calibration flows; motors are always mocked."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from probatio import Invalid
from test_on_off_configuration import coordinator
from test_roller_shutter_v3 import widget
from test_scene_configuration import make_flow, ui_fields

from custom_components.homeassistant_grenton.domain.api.clu_messages.action import GrentonCluApiActionRequest
from custom_components.homeassistant_grenton.domain.entities.roller_shutter_calibration import ShutterCalibration
from custom_components.homeassistant_grenton.dto.widgets.roller_shutter_v3 import GrentonWidgetRollerShutterV3Dto
from custom_components.homeassistant_grenton.mappers.device_mapper import DeviceMapper


CLOCK = 'custom_components.homeassistant_grenton.domain.entities.roller_shutter_calibration.monotonic'


def configured_shutter(tilt=0, **overrides):
    dto = widget().model_dump()
    for name, index in [('mechanicalOffset', 16), ('blindsUpMaxTime', 14), ('blindsDownMaxTime', 15)]:
        dto['object'][name] = dict(cluId='clu1', objectName='ROLLER1', index=str(index), callType='ATTRIBUTE')
        dto['object']['set'+name[0].upper()+name[1:]+'Action'] = dict(
            **dto['object'][name], event='CLICK', value='$value$')
    dto['object']['setLamelMoveTimeoutAction'] = dict(**dto['object']['lamelMoveTimeout'], event='CLICK', value='$value$')
    dto['object']['calibrationAction'] = {**dto['object']['stopAction'], 'index':'8', 'value':'1'}
    coord = coordinator()
    values = {'0': 0, '3': 0, '4': 0, '7': 100, '8': 0, '9': 30000,
              '10': tilt, '13': 0, '14': 22000, '15': 21500, '16': 0, **overrides}
    coord.get_value_for_component.side_effect = lambda source: values.get(source.index)
    coord.async_refresh_clu_state = AsyncMock(return_value=True)
    commands = []
    async def execute(action, **kwargs):
        assert kwargs == {'raise_on_error': True}
        commands.append(GrentonCluApiActionRequest.from_action(action).payload)
        if action.index in ('10', '13', '14', '15', '16') and hasattr(action, 'lua_value'):
            values[action.index] = int(action.lua_value)
            if action.index == '13':
                values['7'] = 100 - values['7']
    coord.execute_action.side_effect = execute
    entity = DeviceMapper.to_domain(GrentonWidgetRollerShutterV3Dto(**dto), coord).entities[0]
    entity.entity_id = 'cover.office'
    return entity, coord, values, commands


def test_manual_configuration_is_native_and_only_capability_is_saved_in_options():
    async def run():
        entity, coord, values, commands = configured_shutter()
        options = await entity.apply_configuration(dict(tilt_enabled=True, lamel_move_timeout=3000,
            reverse_position=True, mechanical_offset=1000, blinds_up_max_time=24000, blinds_down_max_time=600000))
        assert options['entities']['office_shutter'] == {'tilt_enabled': True}
        assert entity._tilt_enabled is True
        assert values['7'] == 0 and entity.current_cover_position == 100
        assert set(commands) == {'ROLLER1:set(10,3000)', 'ROLLER1:set(13,1)', 'ROLLER1:set(16,1000)',
                                'ROLLER1:set(14,24000)', 'ROLLER1:set(15,600000)'}
        commands.clear()
        await entity.apply_configuration({'tilt_enabled': False})
        assert entity._tilt_enabled is False and values['10'] == 0
        assert commands == ['ROLLER1:set(10,0)']
    asyncio.run(run())


@pytest.mark.parametrize('key,value', [('mechanical_offset', 1001), ('mechanical_offset', -1),
    ('blinds_up_max_time', 600001), ('blinds_down_max_time', True), ('reverse_position', 1),
    ('lamel_move_timeout', float('nan')), ('blinds_up_max_time', 0.5)])
def test_settings_validate_everything_before_any_write(key, value):
    async def run():
        entity, coord, _, _ = configured_shutter()
        with pytest.raises(ServiceValidationError):
            await entity.apply_native_settings({'mechanical_offset': 100, key: value})
        coord.execute_action.assert_not_awaited()
    asyncio.run(run())


def test_settings_require_successful_reads_and_confirmed_readback():
    async def run():
        entity, coord, _, commands = configured_shutter()
        coord.async_refresh_clu_state.return_value = False
        with pytest.raises(HomeAssistantError):
            await entity.apply_native_settings({'mechanical_offset': 50})
        assert not commands
        coord.async_refresh_clu_state.return_value = True
        coord.execute_action.side_effect = None
        with pytest.raises(HomeAssistantError, match='confirm'):
            await entity.apply_configuration({'tilt_enabled': True, 'lamel_move_timeout': 1000})
        assert entity._config == {}
    asyncio.run(run())


def test_manual_flow_uses_one_native_form_with_conditional_lamel_time():
    from homeassistant.data_entry_flow import add_visible_conditions_to_serialized_schema, _strip_hidden_fields
    async def run():
        entity, _, _, _ = configured_shutter()
        flow, entry_patch = make_flow(entity.coordinator, entity)
        with entry_patch:
            result = await flow.async_step_entity_list({'entity': entity.entity_id})
            assert result['step_id'] == 'shutter'
            result = await flow.async_step_configure_entity()
            assert result['step_id'] == 'configure_shutter_settings'
            assert result['last_step'] is True
            assert len(entity._get_schema_instance().steps) == 1
            fields = list(ui_fields(result).values())
            add_visible_conditions_to_serialized_schema(result['data_schema'], fields)
            by_name = {field['name']: field for field in fields}
            assert set(by_name) == {'tilt_enabled', 'lamel_move_timeout', 'reverse_position',
                                    'mechanical_offset', 'blinds_up_max_time', 'blinds_down_max_time'}
            assert by_name['lamel_move_timeout']['visible'] == {'field':'tilt_enabled', 'value':True}
            hidden_schema, _ = _strip_hidden_fields(result['data_schema'], {'tilt_enabled':False})
            assert all(key.schema != 'lamel_move_timeout' for key in hidden_schema.schema)
            visible_schema, _ = _strip_hidden_fields(result['data_schema'], {'tilt_enabled':True})
            assert any(key.schema == 'lamel_move_timeout' for key in visible_schema.schema)
            result = await flow.async_step_configure_entity({'tilt_enabled':True, 'lamel_move_timeout':0,
                'reverse_position':False, 'mechanical_offset':0,
                'blinds_up_max_time':0, 'blinds_down_max_time':0})
            assert result['errors']['lamel_move_timeout'] == 'invalid_configuration'
    asyncio.run(run())


def test_legacy_fallback_and_native_state_meanings():
    entity, _, values, _ = configured_shutter(**{'14': 0, '15': 0})
    assert entity.travel_time('up') == entity.travel_time('down') == 30000
    values['9'] = 45000
    assert entity.travel_time('down') == 45000
    for code, name in enumerate(['stopped', 'opening', 'closing', 'blocked', 'calibrating']):
        values['0'] = code
        assert entity.extra_state_attributes['grenton_state'] == name
    values['3'], values['4'] = 1, 0
    assert entity.extra_state_attributes['up_relay_active'] is True
    assert entity.extra_state_attributes['down_relay_active'] is False


def test_manual_settings_are_visible_in_native_options_manager_and_saved_without_reload(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from homeassistant.config_entries import OptionsFlowManager
    from homeassistant.core import HomeAssistant
    async def run():
        entity, coord, values, commands = configured_shutter()
        flow, entry_patch = make_flow(coord, entity)
        entry = coord.config_entry
        entry.entry_id = 'interface1'
        entry.update_listeners = []
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = SimpleNamespace(async_get_known_entry=Mock(return_value=entry),
            async_update_entry=Mock(return_value=True), async_schedule_reload=Mock())
        flow.hass, flow.handler = hass, entry.entry_id
        flow.flow_id = 'shutter-flow'
        manager = OptionsFlowManager(hass)
        manager._async_add_flow_progress(flow)
        with entry_patch:
            await manager._async_handle_step(flow, 'entity_list', {'entity': entity.entity_id})
            await manager.async_configure(flow.flow_id, {'next_step_id':'configure_entity'})
            assert flow.cur_step['step_id'] == 'configure_shutter_settings'
            result = await manager.async_configure(flow.flow_id, {'tilt_enabled':True, 'lamel_move_timeout':1000,
                'reverse_position':True, 'mechanical_offset':500,
                'blinds_up_max_time':24000, 'blinds_down_max_time':25000})
            assert result['type'] == 'create_entry'
            assert result['data']['entities'][entity.unique_id] == {'tilt_enabled':True}
            hass.config_entries.async_schedule_reload.assert_not_called()
            assert values['16'] == 500 and values['13'] == 1
        await hass.async_stop()
    asyncio.run(run())


def test_disabling_tilt_zeroes_native_timeout_and_survives_reload():
    async def run():
        entity, coord, values, _ = configured_shutter(tilt=1000)
        coord.config_entry.options = await entity.apply_configuration({'tilt_enabled': False})
        reloaded = DeviceMapper.to_domain(widget(), coord).entities[0]
        assert reloaded._tilt_enabled is False and values['10'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('hidden_value', [None, 3000, 'stale invalid hidden input'])
def test_native_flow_disabling_tilt_ignores_hidden_values_and_writes_zero(tmp_path, hidden_value):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from homeassistant.config_entries import OptionsFlowManager
    from homeassistant.core import HomeAssistant
    async def run():
        entity, coord, values, commands = configured_shutter(tilt=3000)
        flow, entry_patch = make_flow(coord, entity)
        entry = coord.config_entry
        entry.entry_id = 'interface1'
        entry.update_listeners = []
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = SimpleNamespace(async_get_known_entry=Mock(return_value=entry),
            async_update_entry=Mock(return_value=True), async_schedule_reload=Mock())
        flow.hass, flow.handler = hass, entry.entry_id
        flow.flow_id = 'shutter-flow'
        manager = OptionsFlowManager(hass)
        manager._async_add_flow_progress(flow)
        with entry_patch:
            await manager._async_handle_step(flow, 'entity_list', {'entity':entity.entity_id})
            result = await manager.async_configure(flow.flow_id, {'next_step_id':'configure_entity'})
            assert result['step_id'] == 'configure_shutter_settings' and result['last_step']
            submitted = {'tilt_enabled':False, 'reverse_position':False, 'mechanical_offset':0,
                         'blinds_up_max_time':22000, 'blinds_down_max_time':21500}
            if hidden_value is not None:
                submitted['lamel_move_timeout'] = hidden_value
            result = await manager.async_configure(flow.flow_id, submitted)
            assert result['type'] == 'create_entry'
            assert result['data']['entities'][entity.unique_id] == {'tilt_enabled':False}
            assert values['10'] == 0 and not entity._tilt_enabled
            assert commands == ['ROLLER1:set(10,0)']
        await hass.async_stop()
    asyncio.run(run())


def observe_motor(entity, coord, values):
    original = coord.execute_action.side_effect
    async def execute(action, **kwargs):
        await original(action, **kwargs)
        if action.index == '8':
            values['0'] = 4
            values['3'] = int(action.arguments[0] == '1')
            values['4'] = int(action.arguments[0] == '0')
    coord.execute_action.side_effect = execute


async def finish_motor(session, values):
    # Allow the observer to see calibration before the idle samples.
    await asyncio.sleep(.005)
    values['0'] = values['3'] = values['4'] = 0
    values['7'] = 100 if session.active_direction == 'up' else 0
    await asyncio.wait_for(session.wait(), 1)


def test_cancel_does_not_stop_motor_and_restores_only_after_stable_idle():
    async def run():
        entity, coord, values, commands = configured_shutter(**{'7':0})
        observe_motor(entity, coord, values)
        session = await entity.create_calibration('up',45000)
        session.POLL_SECONDS = .001
        await session.edge_ready()
        await session.measure()
        await session.abort()
        assert session.running and values['0'] == 4 and values['14'] == 45000
        assert not any('execute(3' in x for x in commands)
        await finish_motor(session, values)
        assert session.finished and values['14'] == 22000
        assert session.elapsed_ms is None
    asyncio.run(run())


def test_manual_stopwatch_result_can_be_saved_while_calibration_continues():
    async def run():
        entity, coord, values, commands = configured_shutter(tilt=3000, **{'7':0})
        observe_motor(entity, coord, values)
        clock = [100]
        with patch(CLOCK,side_effect=lambda:clock[0]):
            session = await entity.create_calibration('up',45000)
            session.POLL_SECONDS = .001
            await session.edge_ready()
            await session.measure()
            clock[0] = 124
            await session.mark_tilt_started()
            clock[0] = 127
            await session.stop_measurement()
            assert session.elapsed_ms == 27000 and session.suggested_ms == 24000
            assert session.running and values['0'] == 4
            async with session._lock:
                await entity.apply_native_settings({session.key:24000},calibration=session)
                session._temporary=False
                session.finished=True
                session.phase='saved'
            await finish_motor(session,values)
            assert values['14'] == 24000 and session.phase == 'saved'
            assert not any('execute(3' in x for x in commands)
    asyncio.run(run())


def test_start_requires_the_actual_opposite_edge_and_idle_state():
    async def run():
        entity, _, values, commands = configured_shutter(**{'7':50})
        session = await entity.create_calibration('up',30000)
        with pytest.raises(HomeAssistantError):await session.edge_ready()
        assert not commands
        values['7']=0
        values['0']=4
        with pytest.raises(HomeAssistantError):await session.edge_ready()
        values['0']=0
        await session.edge_ready()
        assert session.phase == 'ready' and not commands
    asyncio.run(run())


def test_waiting_for_position_does_not_accept_an_initial_stale_idle_sample():
    async def run():
        entity, _, values, _ = configured_shutter(**{'7':0})
        session=await entity.create_calibration('up',30000)
        session.POLL_SECONDS=.001
        await session.run(measuring=False)
        await asyncio.sleep(.005)
        assert session.running and session.phase=='positioning'
        values['0']=4
        await asyncio.sleep(.005)
        await finish_motor(session,values)
        assert session.phase=='ready'
    asyncio.run(run())


def test_native_wait_loader_and_measurement_save_while_motor_runs(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from homeassistant.core import HomeAssistant
    async def run():
        entity,coord,values,commands=configured_shutter(**{'7':50})
        observe_motor(entity,coord,values)
        flow,entry_patch=make_flow(coord,entity)
        hass=HomeAssistant(str(tmp_path));hass.config.language='pl'
        flow.hass=hass
        flow.flow_id = 'shutter-flow'
        with entry_patch:
            flow.selected_entity_uid=entity.unique_id
            await flow.async_step_shutter_calibration({'direction':'up'})
            result=await flow.async_step_shutter_limits({'limit_up_ms':45000})
            assert 'shutter_edge_ready' not in result['menu_options']
            assert result['step_id'] == 'shutter_position_close_required'
            session=flow._shutter_session;session.POLL_SECONDS=.001
            result=await flow.async_step_shutter_position_down()
            assert result['type']=='progress' and result['progress_action']=='shutter_down'
            await finish_motor(session,values)
            await asyncio.wait_for(flow._shutter_wait_task,2)
            assert (await flow.async_step_shutter_wait())['type']=='progress_done'
            result=await flow.async_step_shutter_position()
            assert 'shutter_edge_ready' in result['menu_options']
            await flow.async_step_shutter_edge_ready()
            await flow._shutter_edge_task
            assert (await flow.async_step_shutter_check_position())['step_id'] == 'shutter_ready'
            clock=[100]
            with patch(CLOCK,side_effect=lambda:clock[0]):
                result=await flow.async_step_shutter_start_measurement()
                assert result['step_id']=='shutter_measurement'
                clock[0]=127
                result=await flow.async_step_shutter_stop_measurement()
                assert result['step_id']=='shutter_review' and values['0']==4
                result=await flow.async_step_shutter_review({'total_ms':27000,'save':True})
                assert result['type']=='create_entry' and values['14']==27000
                flow.async_remove()
                await finish_motor(session,values)
                assert session.phase=='saved' and values['14']==27000
        assert not any('execute(3' in x for x in commands)
        await hass.async_stop()
    asyncio.run(run())


def test_native_manager_frontend_timer_and_popup_close_keep_firmware_running(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from homeassistant.config_entries import OptionsFlowManager
    from homeassistant.core import HomeAssistant

    async def run():
        entity, coord, values, commands = configured_shutter(**{'7': 100})
        observe_motor(entity, coord, values)
        flow, entry_patch = make_flow(coord, entity)
        entry = coord.config_entry
        entry.entry_id, entry.update_listeners = 'interface1', []
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = SimpleNamespace(async_get_known_entry=Mock(return_value=entry),
            async_update_entry=Mock(return_value=True), async_schedule_reload=Mock())
        flow.hass, flow.handler = hass, entry.entry_id
        flow.flow_id = 'shutter-flow'
        manager = OptionsFlowManager(hass)
        manager._async_add_flow_progress(flow)
        with entry_patch:
            await manager._async_handle_step(flow, 'entity_list', {'entity': entity.entity_id})
            await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_calibration'})
            await manager.async_configure(flow.flow_id, {'direction': 'down'})
            await manager.async_configure(flow.flow_id, {'limit_down_ms': 45000})
            session = flow._shutter_session
            session.POLL_SECONDS = .001
            assert not commands
            result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_edge_ready'})
            assert result['type'] == 'progress'
            await flow._shutter_edge_task
            await asyncio.sleep(.01)  # The native manager advances the completed progress task.
            result = await manager.async_configure(flow.flow_id)
            assert result['step_id'] == 'shutter_ready'
            clock = [100]
            with patch(CLOCK, side_effect=lambda: clock[0]):
                result = await manager.async_configure(flow.flow_id, {'next_step_id': 'shutter_start_measurement'})
                assert result['type'] == 'form'
                timer = ui_fields(result)['measurement']['selector']['grenton_shutter_measurement']
                assert timer['elapsed_ms'] == 0 and timer['direction'] == 'down'
                clock[0] = 102.875
                result = await manager.async_configure(flow.flow_id)
                timer = ui_fields(result)['measurement']['selector']['grenton_shutter_measurement']
                assert timer['elapsed_ms'] == 2875
                manager.async_abort(flow.flow_id)
                await asyncio.sleep(.01)
                assert session.running and values['0'] == 4
                assert session.elapsed_ms is None
                await finish_motor(session, values)
                assert session.finished and values['15'] == 21500
        assert not any('execute(3' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())


def test_close_during_initial_loader_cancels_ui_but_keeps_motor_observation(tmp_path):
    from homeassistant.core import HomeAssistant

    async def run():
        entity, coord, values, commands = configured_shutter(**{'7': 50})
        observe_motor(entity, coord, values)
        flow, entry_patch = make_flow(coord, entity)
        hass = HomeAssistant(str(tmp_path))
        flow.hass = hass
        flow.flow_id = 'shutter-flow'
        with entry_patch:
            flow.selected_entity_uid = entity.unique_id
            session = await entity.create_calibration('up', 30000)
            session.POLL_SECONDS = .001
            flow._shutter_session = session
            await flow.async_step_shutter_position_down()
            await asyncio.sleep(.005)
            flow._shutter_wait_task.cancel()
            flow.async_remove()
            await asyncio.sleep(.005)
            assert session.running and not session._guard.cancelled()
            await finish_motor(session, values)
            assert session.phase == 'cancelled' and session.finished
        assert not any('execute(3' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('direction,position,index,argument', [('up', 0, '14', '1'), ('down', 100, '15', '0')])
def test_failed_calibration_ack_keeps_observer_and_excludes_other_commands(direction, position, index, argument):
    async def run():
        entity, coord, values, commands = configured_shutter(**{'7': position})
        observe_motor(entity, coord, values)
        session = await entity.create_calibration(direction, 45000)
        session.POLL_SECONDS = .001
        with pytest.raises(HomeAssistantError):
            await session.measure()
        await session.edge_ready()
        original = coord.execute_action.side_effect
        async def lost_reply(action, **kwargs):
            await original(action, **kwargs)
            if action.index == '8':
                raise HomeAssistantError('Lost reply')
        coord.execute_action.side_effect = lost_reply
        with pytest.raises(HomeAssistantError, match='Lost reply'):
            await session.measure()
        assert f'ROLLER1:execute(8, {argument})' in commands
        assert session.running and values[index] == 45000
        with pytest.raises(HomeAssistantError):
            await entity.create_calibration(direction, 30000)
        with pytest.raises(ServiceValidationError):
            await entity.apply_native_settings({'mechanical_offset': 100})
        with pytest.raises(ServiceValidationError):
            await entity.async_open_cover()
        await session.abort()
        await finish_motor(session, values)
        assert values[index] == (22000 if direction == 'up' else 21500)
    asyncio.run(run())


def test_failed_provisional_setting_never_starts_motor_and_can_be_cancelled():
    async def run():
        entity, coord, values, commands = configured_shutter(**{'7': 0})
        session = await entity.create_calibration('up', 45000)
        session.POLL_SECONDS = .001
        await session.edge_ready()
        original = coord.execute_action.side_effect
        async def fail_limit(action, **kwargs):
            if getattr(action, 'lua_value', None) == '45000':
                raise HomeAssistantError('Write failed')
            await original(action, **kwargs)
        coord.execute_action.side_effect = fail_limit
        with pytest.raises(HomeAssistantError):
            await session.measure()
        assert not session.running
        assert not any(':execute(8,' in command for command in commands)
        await session.abort()
        assert session.finished and values['14'] == 22000
    asyncio.run(run())


@pytest.mark.parametrize('direction,position,button', [
    ('up', 0, 'shutter_position_down'), ('down', 100, 'shutter_position_up'),
])
def test_positioning_remains_optional_at_the_reported_edge(tmp_path, direction, position, button):
    from homeassistant.core import HomeAssistant

    async def run():
        entity, coord, values, commands = configured_shutter(**{'7': position})
        observe_motor(entity, coord, values)
        flow, entry_patch = make_flow(coord, entity)
        hass = HomeAssistant(str(tmp_path))
        flow.hass = hass
        flow.flow_id = 'shutter-flow'
        with entry_patch:
            flow.selected_entity_uid = entity.unique_id
            session = await entity.create_calibration(direction, 30000)
            session.POLL_SECONDS = .001
            flow._shutter_session = session
            for _ in range(2):
                result = await flow.async_step_shutter_position()
                assert result['menu_options'] == ['shutter_edge_ready', button, 'shutter_cancel']
                result = await getattr(flow, f'async_step_{button}')()
                assert result['type'] == 'progress'
                assert session.running
                with pytest.raises(HomeAssistantError):
                    await session.edge_ready()
                await finish_motor(session, values)
                await flow._shutter_wait_task
                assert (await flow.async_step_shutter_wait())['type'] == 'progress_done'
            await session.abort()
        argument = '0' if direction == 'up' else '1'
        assert commands == [f'ROLLER1:execute(8, {argument})'] * 2
        assert not any('execute(3' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('direction,position,allowed,blocked', [
    ('down', 100, 'shutter_position_up', 'shutter_position_down'),
    ('down', 50, 'shutter_position_up', 'shutter_position_down'),
    ('up', 0, 'shutter_position_down', 'shutter_position_up'),
    ('up', 50, 'shutter_position_down', 'shutter_position_up'),
])
def test_only_opposite_direction_is_offered_and_wrong_handler_cannot_move(
    direction, position, allowed, blocked,
):
    async def run():
        entity, coord, _, commands = configured_shutter(**{'7': position})
        flow, entry_patch = make_flow(coord, entity)
        with entry_patch:
            flow.selected_entity_uid = entity.unique_id
            session = await entity.create_calibration(direction, 30000)
            flow._shutter_session = session
            result = await flow.async_step_shutter_position()
            assert allowed in result['menu_options']
            assert blocked not in result['menu_options']
            result = await getattr(flow, f'async_step_{blocked}')()
            assert result['step_id'] == 'shutter_error'
            assert not commands and not session.running
            await session.abort()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['finish', 'cancel'])
def test_measurement_form_records_backend_time_or_cancels_without_stopping_motor(tmp_path, action):
    from homeassistant.core import HomeAssistant

    async def run():
        entity, coord, values, commands = configured_shutter(**{'7': 0})
        observe_motor(entity, coord, values)
        flow, entry_patch = make_flow(coord, entity)
        hass = HomeAssistant(str(tmp_path))
        flow.hass = hass
        flow.flow_id = 'shutter-flow'
        with entry_patch:
            flow.selected_entity_uid = entity.unique_id
            session = await entity.create_calibration('up', 30000)
            session.POLL_SECONDS = .001
            flow._shutter_session = session
            await session.edge_ready()
            clock = [100]
            with patch(CLOCK, side_effect=lambda: clock[0]):
                result = await flow.async_step_shutter_start_measurement()
                schema = result['data_schema']
                assert schema({}) == {'measurement': 'finish'}
                clock[0] = 127.321
                result = await flow.async_step_shutter_measurement(schema({'measurement': action}))
                if action == 'finish':
                    assert result['step_id'] == 'shutter_review'
                    assert session.elapsed_ms == 27321
                else:
                    assert result['type'] == 'abort'
                    assert session.elapsed_ms is None
                assert session.running and values['0'] == 4
                await session.abort()
                await finish_motor(session, values)
        assert not any('execute(3' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())
