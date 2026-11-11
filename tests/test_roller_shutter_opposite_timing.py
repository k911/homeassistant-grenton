"""Opt-in preservation of opposite-direction total runtime."""

import asyncio

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_roller_shutter_tilt_calibration import tilt_flow


async def review_session(entity, flow, direction):
    session = await entity.create_calibration(direction, 45000)
    session.phase, session.elapsed_ms, session.lamel_ms = 'review', 27000, 3000
    session.phase_change_ms = 3000 if direction == 'up' else 24000
    flow._shutter_session = session
    return session


def submission(lamel, adapt=True):
    return {'timings': {'total_ms': 27000, 'travel_ms': 27000 - lamel,
                        'lamel_move_timeout': lamel, 'adapt_opposite': adapt}}


@pytest.mark.parametrize('direction', ['up', 'down'])
@pytest.mark.parametrize('adapt', [False, True])
@pytest.mark.parametrize('lamel', [2000, 4000, 2345])
def test_only_opted_in_saves_preserve_the_opposite_total(tmp_path, direction, adapt, lamel):
    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, direction)
        with entry_patch:
            await review_session(entity, flow, direction)
            selected, opposite = ('14', '15') if direction == 'up' else ('15', '14')
            before = dict(values)
            result = await flow.async_step_shutter_review(submission(lamel, adapt))
            assert result['step_id'] == 'shutter_calibrated'
            assert values[selected] == 27000 - lamel and values['10'] == lamel
            expected = before[opposite] + (before['10'] - lamel if adapt else 0)
            assert values[opposite] == expected
            if adapt:
                assert values[opposite] + lamel == before[opposite] + before['10']
                assert f'ROLLER1:set({opposite},{expected})' in commands
            else:
                assert not any(command.startswith(f'ROLLER1:set({opposite},') for command in commands)
            assert entity._config == {'tilt_enabled': True}
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('opposite,lamel', [(1000, 4000), (500, 4000), (600000, 2000)])
def test_out_of_range_compensation_is_rejected_before_any_write(tmp_path, opposite, lamel):
    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, 'down')
        values['14'] = opposite
        with entry_patch:
            session = await review_session(entity, flow, 'down')
            before = dict(values)
            result = await flow.async_step_shutter_review(submission(lamel))
            assert result['errors']['base'] == 'cannot_apply_configuration'
            assert values == before and not commands
            assert session.phase == 'review' and not session.finished
            await session.abort()
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('lamel,expected', [(2000, 31000), (3000, 0)])
def test_zero_opposite_timeout_uses_legacy_fallback_only_when_tilt_changes(tmp_path, lamel, expected):
    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, 'down')
        values['14'], values['9'] = 0, 30000
        with entry_patch:
            await review_session(entity, flow, 'down')
            result = await flow.async_step_shutter_review(submission(lamel))
            assert result['step_id'] == 'shutter_calibrated'
            assert values['14'] == expected
            assert entity.travel_time('up') + values['10'] == 33000
            if lamel == 3000:
                assert not any(command.startswith('ROLLER1:set(14,') for command in commands)
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['write', 'readback'])
def test_retry_keeps_the_original_total_after_a_partial_save(tmp_path, failure):
    async def run():
        entity, coord, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, 'down')
        with entry_patch:
            session = await review_session(entity, flow, 'down')
            original = coord.execute_action.side_effect
            failed = [False]
            async def execute(action, **kwargs):
                if failure == 'write' and action.index == '14' and not failed[0]:
                    failed[0] = True
                    raise HomeAssistantError('Lost write')
                await original(action, **kwargs)
            async def read(*args, **kwargs):
                indices = {source.index for source in kwargs['required_states']}
                if failure == 'readback' and indices == {'10', '14', '15'} and not failed[0]:
                    failed[0] = True
                    return False
                return True
            coord.execute_action.side_effect = execute
            coord.async_refresh_clu_state.side_effect = read
            result = await flow.async_step_shutter_review(submission(2000))
            assert result['errors']['base'] == 'cannot_apply_configuration'
            assert values['10'] == 2000  # The shared tilt write succeeded before the failure.
            assert flow._shutter_pending_opposite.total_ms == 25000
            result = await flow.async_step_shutter_review(submission(2000))
            assert result['step_id'] == 'shutter_calibrated'
            assert values['14'] == 23000 and values['14'] + values['10'] == 25000
            assert commands.count('ROLLER1:set(14,23000)') == 1
            assert flow._shutter_pending_opposite is None and session.phase == 'saved'
        await hass.async_stop()
    asyncio.run(run())


def test_adjustment_saves_take_a_fresh_baseline_and_reset_the_checkbox(tmp_path):
    async def run():
        entity, _, values, _, flow, entry_patch, hass = tilt_flow(tmp_path, 'up')
        with entry_patch:
            session = await review_session(entity, flow, 'up')
            session.phase, session.finished = 'saved', True
            for lamel in (2000, 3500):
                result = await flow.async_step_shutter_tilt_adjust()
                assert result['data_schema']({})['timings']['adapt_opposite'] is False
                result = await flow.async_step_shutter_tilt_adjust(submission(lamel))
                assert result['step_id'] == 'shutter_calibrated'
                assert values['15'] + values['10'] == 24500
                assert flow._shutter_pending_opposite is None
        await hass.async_stop()
    asyncio.run(run())


def test_compensation_can_be_saved_while_native_calibration_continues(tmp_path):
    from unittest.mock import patch
    from test_roller_shutter_configuration import CLOCK, finish_motor

    async def run():
        entity, _, values, commands, flow, entry_patch, hass = tilt_flow(tmp_path, 'down')
        with entry_patch:
            session = await entity.create_calibration('down', 45000)
            session.POLL_SECONDS = .001
            flow._shutter_session = session
            await session.edge_ready()
            clock = [100]
            with patch(CLOCK, side_effect=lambda: clock[0]):
                await session.measure()
                assert values['15'] == 45000
                clock[0] = 124
                await session.mark_tilt_started()
                clock[0] = 127
                await session.stop_measurement()
                result = await flow.async_step_shutter_review(submission(2000))
                assert result['step_id'] == 'shutter_calibrated'
                assert session.running and values['0'] == 4
                assert values['15'] == 25000 and values['14'] == 23000 and values['10'] == 2000
                await flow.async_step_shutter_done()
                await finish_motor(session, values)
                assert values['15'] == 25000 and values['14'] + values['10'] == 25000
        assert not any('execute(3' in command for command in commands)
        await hass.async_stop()
    asyncio.run(run())
