"""V3 bindings, cover semantics and commands, using a synthetic office widget."""

import asyncio
from copy import deepcopy
import json
import logging
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import EntityPlatform
from test_clu_scripts import make_entry, make_hass
from test_on_off_configuration import coordinator

from custom_components.homeassistant_grenton import cover
from custom_components.homeassistant_grenton.domain.api.clu_messages.action import GrentonCluApiActionRequest
from custom_components.homeassistant_grenton.dto.widgets.roller_shutter_v3 import GrentonWidgetRollerShutterV3Dto
from custom_components.homeassistant_grenton.mappers.device_mapper import DeviceMapper


def widget():
    bindings = {}
    for name, index in {'state': 0, 'up': 3, 'down': 4, 'position': 7,
                        'lamelPosition': 8, 'lamelMoveTimeout': 10,
                        'reversePosition': 13}.items():
        bindings[name] = dict(cluId='clu1', objectName='ROLLER1', index=str(index), callType='ATTRIBUTE')
    for name, index, value in [('setPositionAction', 10, '$value$'),
                               ('setLamelPositionAction', 9, '$value$'),
                               ('moveUpAction', 0, '0'), ('moveDownAction', 1, '0'),
                               ('startAction', 2, '0'), ('stopAction', 3, ''),
                               ('setRollerBlockedAction', 13, '0')]:
        bindings[name] = dict(cluId='clu1', objectName='ROLLER1', index=str(index),
                              callType='METHOD', event='CLICK', value=value)
    return GrentonWidgetRollerShutterV3Dto(id='office_shutter', icon='blinds', label='Office shutter', object=bindings)


def shutter(**overrides):
    coord = coordinator()
    values = {'0': 0, '7': 100, '8': 0, '10': 0, '13': 0, **overrides}
    coord.get_value_for_component.side_effect = lambda source: values.get(source.index)
    device = DeviceMapper.to_domain(widget(), coord)
    return device.entities[0], coord, values


def test_office_widget_maps_to_one_cover_without_tilt_for_zero_lamel_timeout():
    entity, coord, values = shutter()
    assert entity.unique_id == 'office_shutter'
    assert entity.device_info.name == 'Office shutter'
    assert entity.current_cover_position == 100
    assert entity.is_closed is False
    assert entity.is_opening is entity.is_closing is False
    assert entity.current_cover_tilt_position is None
    assert entity.supported_features == (CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE |
                                          CoverEntityFeature.SET_POSITION | CoverEntityFeature.STOP)
    assert {x.index for x in entity.state_sources} == {'0', '7'}
    slow = {call.args[0].index for call in coord.register_component_state.call_args_list
            if call.kwargs.get('read_mode') == 'slow'}
    assert slow == {'9', '10', '13'}
    values['10'] = 1000
    assert entity.supported_features & CoverEntityFeature.SET_TILT_POSITION
    assert entity.current_cover_tilt_position == 100
    assert {x.index for x in entity.state_sources} == {'0', '7', '8'}


@pytest.mark.parametrize('bad', [None, True, 'bad', float('nan'), float('inf'), -1, 255])
def test_invalid_position_is_unknown_instead_of_open_or_raising(bad):
    entity, _, _ = shutter(**{'7': bad})
    assert entity.current_cover_position is None
    assert entity.is_closed is None


@pytest.mark.parametrize('state, opening, closing', [(None, None, None), ('bad', None, None),
    (0, False, False), (1, True, False), (2, False, True), (3, False, False), (4, False, False)])
def test_movement_flags(state, opening, closing):
    entity, _, _ = shutter(**{'0': state})
    assert entity.is_opening is opening
    assert entity.is_closing is closing


@pytest.mark.parametrize('native, expected', [(0, 100), (45, 50), (90, 0), (255, None), (None, None)])
def test_lamel_angle_converts_to_ha_open_percentage(native, expected):
    entity, _, _ = shutter(**{'10': 1000, '8': native})
    assert entity.current_cover_tilt_position == expected


def test_reverse_position_normalizes_both_reads_and_writes():
    entity, coord, values = shutter(**{'7': 0, '13': 1})
    assert entity.current_cover_position == 100
    assert entity.is_closed is False
    asyncio.run(entity.async_set_cover_position(position=25))
    assert coord.execute_action.call_args.args[0].value == '75'
    values['7'] = 100
    assert entity.is_closed is True
    values['13'] = None
    assert entity.current_cover_position is None
    with pytest.raises(ServiceValidationError):
        asyncio.run(entity.async_set_cover_position(position=25))


def test_commands_match_imported_bindings_and_do_not_mutate_templates():
    entity, coord, _ = shutter(**{'10': 1000})
    position_template, tilt_template = deepcopy(entity.set_position), deepcopy(entity.set_tilt_position)
    payloads = []
    async def send(action, **kwargs):
        assert kwargs == {'raise_on_error': True}
        await asyncio.sleep(0)
        payloads.append(GrentonCluApiActionRequest.from_action(action).payload)
    coord.execute_action.side_effect = send
    async def run():
        await entity.async_open_cover()
        await entity.async_close_cover()
        await entity.async_stop_cover()
        await asyncio.gather(entity.async_set_cover_position(position=25), entity.async_set_cover_position(position=75))
        await entity.async_open_cover_tilt()
        await entity.async_close_cover_tilt()
        await entity.async_set_cover_tilt_position(tilt_position=50)
        await entity.async_stop_cover_tilt()
    asyncio.run(run())
    assert payloads == ['ROLLER1:execute(10,"100")', 'ROLLER1:execute(10,"0")',
                        'ROLLER1:execute(3,"")', 'ROLLER1:execute(10,"25")',
                        'ROLLER1:execute(10,"75")', 'ROLLER1:execute(9,"0")',
                        'ROLLER1:execute(9,"90")', 'ROLLER1:execute(9,"45")', 'ROLLER1:execute(3,"")']
    assert entity.set_position == position_template
    assert entity.set_tilt_position == tilt_template


@pytest.mark.parametrize('value', [None, True, '50', -1, 101, float('nan'), float('inf')])
def test_bad_service_input_never_sends_a_command(value):
    entity, coord, _ = shutter(**{'10': 1000})
    for method, key in [(entity.async_set_cover_position, 'position'), (entity.async_set_cover_tilt_position, 'tilt_position')]:
        with pytest.raises(ServiceValidationError):
            asyncio.run(method(**{key: value}))
    coord.execute_action.assert_not_awaited()


def test_disabled_tilt_cannot_be_called_and_command_errors_reach_caller():
    entity, coord, _ = shutter()
    with pytest.raises(ServiceValidationError):
        asyncio.run(entity.async_open_cover_tilt())
    coord.execute_action.assert_not_awaited()
    coord.execute_action.side_effect = HomeAssistantError('timeout')
    with pytest.raises(HomeAssistantError, match='timeout'):
        asyncio.run(entity.async_close_cover())


def test_script_position_template_preserves_fixed_arguments():
    dto = widget().model_dump()
    dto['object']['setPositionAction'] = dict(cluId='clu1', objectName='MoveGroup', callType='SCRIPT',
                                              event='CLICK', value='"office",$value$')
    coord = coordinator()
    coord.get_value_for_component.return_value = 0
    entity = DeviceMapper.to_domain(GrentonWidgetRollerShutterV3Dto(**dto), coord).entities[0]
    asyncio.run(entity.async_set_cover_position(position=40))
    action = coord.execute_action.call_args.args[0]
    assert GrentonCluApiActionRequest.from_action(action).payload == 'MoveGroup("office",40)'
    assert entity.set_position.value == '"office",$value$'


@pytest.mark.parametrize('language, name', [('pl', 'Roleta'), ('en', 'Roller shutter')])
def test_native_ha_cover_registration_and_availability(tmp_path, language, name):
    async def run():
        entry = make_entry()
        hass = await make_hass(tmp_path, [entry])
        entity, coord, values = shutter()
        coord.config_entry = entry
        platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain='cover',
                                  platform_name='grenton', platform=None, scan_interval=timedelta(seconds=30),
                                  entity_namespace=None)
        platform.config_entry = entry
        translations = json.loads((Path(__file__).parents[1] / 'custom_components' /
            'homeassistant_grenton' / 'translations' / f'{language}.json').read_text())
        translated_name = translations['entity']['cover']['roller_shutter']['name']
        assert translated_name == name
        platform_translations = {'component.grenton.entity.cover.roller_shutter.name': translated_name}
        platform.platform_data.platform_translations = platform_translations
        platform.platform_data.object_id_platform_translations = platform_translations
        platform.platform_data.default_language_platform_translations = platform_translations
        try:
            await platform.async_add_entities([entity])
            state = hass.states.get(entity.entity_id)
            assert entity.registry_entry.original_name == name
            assert entity.registry_entry.translation_key == 'roller_shutter'
            assert entity.unique_id == 'office_shutter'
            assert state.state == 'open'
            assert state.attributes['current_position'] == 100
            assert 'current_tilt_position' not in state.attributes
            values['7'] = 0
            entity._handle_coordinator_update()
            assert hass.states.get(entity.entity_id).state == 'closed'
            values['0'] = 1
            entity._handle_coordinator_update()
            assert hass.states.get(entity.entity_id).state == 'opening'
            coord.states_available.return_value = False
            entity._handle_coordinator_update()
            assert hass.states.get(entity.entity_id).state == 'unavailable'
        finally:
            await platform.async_reset()
            await hass.async_stop()
    asyncio.run(run())
