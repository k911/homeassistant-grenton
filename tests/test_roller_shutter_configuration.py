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
        assert entity._tilt_enabled is False and values['10'] == 3000
        assert not commands
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


def test_manual_flow_hides_lamel_time_until_enabled_and_never_offers_legacy_max_time():
    async def run():
        entity, _, _, _ = configured_shutter()
        flow, entry_patch = make_flow(entity.coordinator, entity)
        with entry_patch:
            result = await flow.async_step_entity_list({'entity': entity.entity_id})
            assert result['step_id'] == 'shutter'
            result = await flow.async_step_configure_entity()
            assert result['step_id'] == 'configure_shutter_tilt'
            result = await flow.async_step_configure_entity({'tilt_enabled': False})
            fields = ui_fields(result)
            assert 'lamel_move_timeout' not in fields
            assert set(fields) == {'reverse_position', 'mechanical_offset', 'blinds_up_max_time', 'blinds_down_max_time'}
        entity2, _, _, _ = configured_shutter()
        flow, entry_patch = make_flow(entity2.coordinator, entity2)
        with entry_patch:
            flow.selected_entity_uid = entity2.unique_id
            await flow.async_step_configure_entity()
            result = await flow.async_step_configure_entity({'tilt_enabled': True})
            assert 'lamel_move_timeout' in ui_fields(result)
            result = await flow.async_step_configure_entity({'lamel_move_timeout': 0, 'reverse_position': False,
                'mechanical_offset': 0, 'blinds_up_max_time': 0, 'blinds_down_max_time': 0})
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


@pytest.mark.parametrize('direction, opposite_value, measured_value', [('up','0','1'),('down','1','0')])
def test_direction_stopwatch_subtracts_lamels_restores_limit_and_saves_only_on_confirmation(direction, opposite_value, measured_value):
    async def run():
        entity, _, values, commands = configured_shutter(tilt=3000)
        clock = [100]
        with patch(CLOCK, side_effect=lambda: clock[0]):
            session = await entity.create_calibration(direction, 40000, 3000)
            await session.run(measuring=False)
            await session.run(measuring=False)
            assert commands.count(f'ROLLER1:execute(8, {opposite_value})') == 2
            assert not any(':execute(0,' in x or ':execute(1,' in x for x in commands)
            await session.edge_ready()
            original = values['14' if direction=='up' else '15']
            await session.measure()
            assert commands[-1] == f'ROLLER1:execute(8, {measured_value})'
            assert values['14' if direction=='up' else '15'] == 40000
            clock[0] = 127
            await session.stop_measurement()
            assert values['14' if direction=='up' else '15'] == original
            assert session.elapsed_ms == 27000 and session.suggested_ms == 24000
            await entity.apply_native_settings({session.key: session.suggested_ms}, calibration=session)
            session.finished = True
            assert values['14' if direction=='up' else '15'] == 24000
    asyncio.run(run())


def test_flow_runs_no_motor_until_button_and_requires_save_confirmation():
    async def run():
        entity, coord, values, commands = configured_shutter(tilt=3000)
        flow, entry_patch = make_flow(coord, entity)
        clock = [100]
        with entry_patch, patch(CLOCK, side_effect=lambda: clock[0]):
            flow.selected_entity_uid = entity.unique_id
            await flow.async_step_shutter_calibration()
            await flow.async_step_shutter_calibration({'direction': 'up'})
            result = await flow.async_step_shutter_limits({'limit_ms': 40000, 'lamel_move_timeout': 3000})
            assert result['step_id'] == 'shutter_position'
            assert not commands
            await flow.async_step_shutter_position_motor()
            await flow.async_step_shutter_edge_ready()
            result = await flow.async_step_shutter_start_measurement()
            assert result['step_id'] == 'shutter_measurement'
            clock[0] = 127
            result = await flow.async_step_shutter_stop_measurement()
            assert result['step_id'] == 'shutter_review'
            assert result['description_placeholders']['suggested_ms'] == '24000'
            assert values['14'] == 22000
            result = await flow.async_step_shutter_review({'total_ms': 27000, 'lamel_move_timeout': 3000, 'save': True})
            assert result['type'] == 'create_entry'
            assert values['14'] == 24000
            assert 'blinds_up_max_time' not in result['data'].get('entities', {}).get(entity.unique_id, {})
    asyncio.run(run())


def test_cancel_and_external_stop_restore_temporary_settings_without_a_result():
    async def run():
        entity, _, values, commands = configured_shutter()
        session = await entity.create_calibration('down', 45000)
        await session.edge_ready()
        await session.measure()
        await entity.async_stop_cover()
        assert session.finished and session.phase == 'cancelled'
        assert values['15'] == 21500
        assert session.elapsed_ms is None and not session.running
        assert 'ROLLER1:execute(3,"")' in commands
    asyncio.run(run())


def test_deadline_stops_abandoned_measurement_and_restores_limit():
    async def run():
        entity, _, values, _ = configured_shutter()
        session = await entity.create_calibration('up', 100)
        session.MARGIN_MS = 0
        await session.edge_ready()
        await session.measure()
        await asyncio.wait_for(session._guard, timeout=1)
        assert session.finished and session.phase == 'expired'
        assert session.elapsed_ms is None and not session.running
        assert values['14'] == 22000
    asyncio.run(run())


def test_failed_calibration_ack_still_sends_stop_and_restores():
    async def run():
        entity, coord, values, commands = configured_shutter()
        original = coord.execute_action.side_effect
        async def execute(action, **kwargs):
            if action.index == '8':
                raise HomeAssistantError('lost reply')
            await original(action, **kwargs)
        coord.execute_action.side_effect = execute
        session = await entity.create_calibration('up', 45000)
        await session.edge_ready()
        with pytest.raises(HomeAssistantError):
            await session.measure()
        await session.abort()
        assert values['14'] == 22000
        assert 'ROLLER1:execute(3,"")' in commands
        assert not session.running
    asyncio.run(run())


def test_calibration_locks_other_writes_and_requires_edge_confirmation():
    async def run():
        entity, _, _, _ = configured_shutter()
        session = await entity.create_calibration('up', 45000)
        with pytest.raises(HomeAssistantError):
            await session.measure()
        with pytest.raises(HomeAssistantError):
            await entity.create_calibration('down', 45000)
        with pytest.raises(ServiceValidationError):
            await entity.apply_native_settings({'mechanical_offset': 10})
        await session.run(measuring=False)
        with pytest.raises(ServiceValidationError):
            await entity.async_open_cover()
        await session.abort()
    asyncio.run(run())


def test_native_options_manager_routes_motor_buttons_and_stops_on_popup_close(tmp_path):
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
        flow.hass = hass
        flow.handler = entry.entry_id
        manager = OptionsFlowManager(hass)
        manager._async_add_flow_progress(flow)
        with entry_patch:
            result = await manager._async_handle_step(flow, 'entity_list', {'entity': entity.entity_id})
            assert result['step_id'] == 'shutter'
            result = await manager.async_configure(flow.flow_id, {'next_step_id':'shutter_calibration'})
            assert result['step_id'] == 'shutter_calibration'
            await manager.async_configure(flow.flow_id, {'direction':'down'})
            result = await manager.async_configure(flow.flow_id, {'limit_ms':45000})
            assert result['step_id'] == 'shutter_position' and not commands
            await manager.async_configure(flow.flow_id, {'next_step_id':'shutter_edge_ready'})
            result = await manager.async_configure(flow.flow_id, {'next_step_id':'shutter_start_measurement'})
            assert result['step_id'] == 'shutter_measurement'
            assert flow._shutter_session.running and values['15'] == 45000
            manager.async_abort(flow.flow_id)
            await hass.async_block_till_done()
            assert flow._shutter_session.finished and not flow._shutter_session.running
            assert values['15'] == 21500
        await hass.async_stop()
    asyncio.run(run())


def test_discard_adjustment_and_invalid_tilt_subtraction_never_save_unconfirmed_result():
    async def run():
        entity, coord, values, _ = configured_shutter(tilt=3000)
        flow, entry_patch = make_flow(coord, entity)
        with entry_patch, patch(CLOCK, side_effect=[100, 127]):
            flow.selected_entity_uid = entity.unique_id
            session = await entity.create_calibration('up', 45000, 3000)
            flow._shutter_session = session
            await session.edge_ready()
            await session.measure()
            await session.stop_measurement()
            result = await flow.async_step_shutter_review({'total_ms':2000, 'lamel_move_timeout':3000, 'save':True})
            assert result['errors'] and values['14'] == 22000
            result = await flow.async_step_shutter_review({'total_ms':28000, 'lamel_move_timeout':3000, 'save':False})
            assert result['step_id'] == 'shutter' and values['14'] == 22000
    asyncio.run(run())


def test_a_failed_stop_keeps_the_deadline_and_can_be_retried():
    async def run():
        entity, coord, values, _ = configured_shutter()
        session = await entity.create_calibration('up', 45000)
        await session.edge_ready()
        await session.measure()
        original = coord.execute_action.side_effect
        async def fail_stop(action, **kwargs):
            if action.index == '3':
                raise HomeAssistantError('stop failed')
            await original(action, **kwargs)
        coord.execute_action.side_effect = fail_stop
        with pytest.raises(HomeAssistantError):
            await session.abort()
        assert session.running and session._guard is not None
        coord.execute_action.side_effect = original
        await session.abort()
        assert not session.running and values['14'] == 22000
    asyncio.run(run())


def test_expired_measurement_is_never_offered_as_a_successful_time():
    async def run():
        entity, _, _, _ = configured_shutter()
        with patch(CLOCK, side_effect=[100, 150]):
            session = await entity.create_calibration('up', 1000)
            await session.edge_ready()
            await session.measure()
            with pytest.raises(HomeAssistantError, match='limit'):
                await session.stop_measurement()
            assert session.elapsed_ms is None and session.finished
    asyncio.run(run())


def test_repeating_edge_position_keeps_guard_if_previous_stop_fails():
    async def run():
        entity, coord, _, _ = configured_shutter()
        session = await entity.create_calibration('up', 45000)
        await session.run(measuring=False)
        guard = session._guard
        original = coord.execute_action.side_effect
        coord.execute_action.side_effect = HomeAssistantError('failed stop')
        with pytest.raises(HomeAssistantError):
            await session.run(measuring=False)
        assert session._guard is guard and not guard.cancelled()
        coord.execute_action.side_effect = original
        await session.abort()
    asyncio.run(run())


def test_failed_provisional_setting_never_starts_motor_and_can_be_cancelled():
    async def run():
        entity, coord, values, commands = configured_shutter()
        session = await entity.create_calibration('up', 45000)
        await session.edge_ready()
        original = coord.execute_action.side_effect
        async def fail_limit(action, **kwargs):
            if getattr(action, 'lua_value', None) == '45000':
                raise HomeAssistantError('write failed')
            await original(action, **kwargs)
        coord.execute_action.side_effect = fail_limit
        with pytest.raises(HomeAssistantError):
            await session.measure()
        assert session.phase == 'failed' and not session.running
        assert not any(':execute(8,' in x for x in commands)
        await session.abort()
        assert values['14'] == 22000
    asyncio.run(run())


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
        manager = OptionsFlowManager(hass)
        manager._async_add_flow_progress(flow)
        with entry_patch:
            await manager._async_handle_step(flow, 'entity_list', {'entity': entity.entity_id})
            await manager.async_configure(flow.flow_id, {'next_step_id':'configure_entity'})
            result = await manager.async_configure(flow.flow_id, {'tilt_enabled':True})
            assert result['step_id'] == 'configure_shutter_settings'
            result = await manager.async_configure(flow.flow_id, {'lamel_move_timeout':1000,
                'reverse_position':True, 'mechanical_offset':500,
                'blinds_up_max_time':24000, 'blinds_down_max_time':25000})
            assert result['type'] == 'create_entry'
            assert result['data']['entities'][entity.unique_id] == {'tilt_enabled':True}
            hass.config_entries.async_schedule_reload.assert_not_called()
            assert values['16'] == 500 and values['13'] == 1
        await hass.async_stop()
    asyncio.run(run())


def test_stop_deadline_retries_after_a_lost_stop_reply():
    async def run():
        entity, coord, values, _ = configured_shutter()
        session = await entity.create_calibration('up', 1)
        session.MARGIN_MS = 0
        original = coord.execute_action.side_effect
        attempts = [0]
        async def execute(action, **kwargs):
            if action.index == '3':
                attempts[0] += 1
                if attempts[0] == 1:
                    raise HomeAssistantError('lost stop reply')
            await original(action, **kwargs)
        real_sleep = asyncio.sleep
        async def sleep(delay):
            await real_sleep(0 if delay == 2 else delay)
        with patch('custom_components.homeassistant_grenton.domain.entities.roller_shutter_calibration.asyncio.sleep', sleep):
            await session.edge_ready()
            await session.measure()
            coord.execute_action.side_effect = execute
            await asyncio.wait_for(session._guard, timeout=1)
        assert attempts[0] == 2 and session.finished
        assert not session.running and values['14'] == 22000
    asyncio.run(run())


def test_native_entity_unload_stops_motor_and_restores_temporary_limit(tmp_path):
    import logging
    from datetime import timedelta
    from homeassistant.helpers.entity_platform import EntityPlatform
    from test_clu_scripts import make_entry, make_hass
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        entity, coord, values, _ = configured_shutter()
        coord.config_entry = entry
        platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain='cover',
            platform_name='grenton', platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
        platform.config_entry = entry
        await platform.async_add_entities([entity])
        session = await entity.create_calibration('up', 45000)
        await session.edge_ready()
        await session.measure()
        assert values['14'] == 45000
        await platform.async_reset()
        assert not session.running and session.finished
        assert values['14'] == 22000
        await hass.async_stop()
    asyncio.run(run())


def test_explicit_tilt_choice_survives_reload_and_is_independent_of_native_timeout():
    async def run():
        entity, coord, values, _ = configured_shutter(tilt=1000)
        coord.config_entry.options = await entity.apply_configuration({'tilt_enabled': False})
        reloaded = DeviceMapper.to_domain(widget(), coord).entities[0]
        assert reloaded._tilt_enabled is False and values['10'] == 1000
    asyncio.run(run())
