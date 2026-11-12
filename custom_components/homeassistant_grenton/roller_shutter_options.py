"""Native options-flow steps for user-directed shutter calibration."""

import logging
import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .domain.entities.roller_shutter_configuration import OppositeTimingAdjustment, milliseconds, number_selector
from .shutter_measurement import ShutterMeasurementSelector
from .shutter_timings import ShutterTimingsSelector


_LOGGER = logging.getLogger(__name__)


class RollerShutterOptionsMixin:
    """Calibration never starts from opening a dialog or submitting settings."""

    @callback
    def async_remove(self):
        """Discard an unfinished measurement while firmware continues moving."""
        super().async_remove()
        session = getattr(self, '_shutter_session', None)
        if session is not None and session.phase != 'saved' and (not session.finished or session.running or session._temporary):
            async def cleanup():
                try:
                    await session.abort()
                except HomeAssistantError:
                    # Firmware observation continues independently of the dialog.
                    _LOGGER.exception('Could not discard calibration after closing its flow')
            self.hass.async_create_task(cleanup(), 'grenton shutter calibration cleanup')

    def _shutter(self):
        from .domain.entities.roller_shutter_v3 import GrentonEntityRollerShutterV3
        entity = next((e for e in self._get_configurable_entities()
                       if e.unique_id == self.selected_entity_uid), None)
        if not isinstance(entity, GrentonEntityRollerShutterV3):
            raise HomeAssistantError('Shutter is no longer available')
        return entity

    def _shutter_menu(self, step, choices, **placeholders):
        return self.async_show_menu(step_id=step, menu_options=choices,
                                   description_placeholders={'entity_name': self._shutter().label, **placeholders})

    async def async_step_shutter(self, user_input=None):
        entity = self._shutter()
        choices = ['configure_entity']
        if entity.calibration_available:
            choices += ['shutter_calibration']
        return self._shutter_menu('shutter', choices)

    async def async_step_shutter_calibration(self, user_input=None):
        entity = self._shutter()
        if not entity.calibration_available:
            return self.async_abort(reason='calibration_unavailable')
        session = entity._calibration
        if session is not None and (not session.finished or session.running or session._temporary):
            self._shutter_session = session
            if session.finished or session.phase == 'cancelling':
                return await self.async_step_shutter_wait()
            if session.phase in ('measuring', 'tilting', 'travelling'):
                return await self.async_step_shutter_measurement()
            if session.phase == 'review':
                return await self.async_step_shutter_review()
            if session.phase == 'ready':
                return await self.async_step_shutter_ready()
            return await self.async_step_shutter_position()
        errors = {}
        if user_input is None:
            try:
                await entity.async_prepare_configuration()
            except HomeAssistantError:
                errors['base'] = 'cannot_read_configuration'
        else:
            direction = user_input.get('direction')
            if direction in ('up', 'down'):
                limit_field = f'limit_{direction}_ms'
                try:
                    limit = milliseconds(user_input.get(limit_field, entity.travel_time(direction)))
                    if limit <= 0:
                        raise vol.Invalid('Enter a positive movement time', path=[limit_field])
                    if 'tilt_enabled' in user_input:
                        enabled = user_input['tilt_enabled']
                        settings = {'tilt_enabled': enabled}
                        if enabled:
                            try:
                                timeout = milliseconds(user_input.get('lamel_move_timeout',
                                    entity.setting_values.get('lamel_move_timeout', 0)))
                            except vol.Invalid as error:
                                raise vol.Invalid(str(error), path=['lamel_move_timeout']) from error
                            if timeout <= 0:
                                raise vol.Invalid('Enter a positive slat movement time', path=['lamel_move_timeout'])
                            settings['lamel_move_timeout'] = timeout
                        options = await entity.apply_configuration(settings)
                        if options != dict(self.config_entry.options):
                            self.hass.config_entries.async_update_entry(self.config_entry, options=options)
                    self._shutter_session = await entity.create_calibration(direction, limit)
                except vol.Invalid as error:
                    field = str(error.path[0]) if error.path else limit_field
                    errors[field] = 'invalid_configuration'
                except ValueError:
                    errors['base'] = 'invalid_configuration'
                except HomeAssistantError:
                    errors['base'] = 'cannot_apply_configuration'
                else:
                    return await self.async_step_shutter_position()
            else:
                errors['direction'] = 'invalid_configuration'
        fields = {
            vol.Required('direction', default=(user_input or {}).get('direction', 'up')): selector.SelectSelector(selector.SelectSelectorConfig(
                options=['up', 'down'], translation_key='shutter_directions',
                mode=selector.SelectSelectorMode.DROPDOWN,
            )),
        }
        for direction in ('up', 'down'):
            field = f'limit_{direction}_ms'
            fields[data_entry_flow.Optional(field,
                default=(user_input or {}).get(field, entity.travel_time(direction)),
                visible={'field': 'direction', 'value': direction})] = number_selector()
        fields[vol.Required('tilt_enabled', default=(user_input or {}).get('tilt_enabled', entity._tilt_enabled))] = selector.BooleanSelector()
        if 'lamel_move_timeout' in entity.setting_actions:
            fields[data_entry_flow.Required('lamel_move_timeout',
                default=(user_input or {}).get('lamel_move_timeout', entity.setting_values.get('lamel_move_timeout', 0)),
                visible={'field': 'tilt_enabled', 'value': True})] = number_selector()
        return self.async_show_form(step_id='shutter_calibration', data_schema=vol.Schema(fields), errors=errors)

    async def async_step_shutter_position(self, user_input=None):
        session = self._shutter_session
        if session.running:
            return await self.async_step_shutter_wait()
        await session.refresh()
        button = 'shutter_position_down' if session.direction == 'up' else 'shutter_position_up'
        closed = session.direction == 'up'
        if session.at_edge:
            choices = ['shutter_edge_ready', button, 'shutter_cancel']
            step = 'shutter_position_closed' if closed else 'shutter_position_open'
        else:
            choices = [button, 'shutter_cancel']
            step = 'shutter_position_close_required' if closed else 'shutter_position_open_required'
        return self._shutter_menu(step, choices)

    async_step_shutter_position_closed = async_step_shutter_position
    async_step_shutter_position_open = async_step_shutter_position
    async_step_shutter_position_close_required = async_step_shutter_position
    async_step_shutter_position_open_required = async_step_shutter_position

    async def async_step_shutter_wait(self, user_input=None):
        session = self._shutter_session
        task = getattr(self, '_shutter_wait_task', None)
        if task is None:
            task = self.hass.async_create_task(session.wait(), 'grenton shutter wait')
            self._shutter_wait_task = task
        if task.done():
            self._shutter_wait_task = None
            try:
                task.result()
            except HomeAssistantError:
                return self.async_show_progress_done(next_step_id='shutter_error')
            next_step = getattr(self, '_shutter_next_after_wait', None)
            self._shutter_next_after_wait = None
            return self.async_show_progress_done(next_step_id=next_step or (
                'shutter_calibration' if session.finished else 'shutter_position'))
        return self.async_show_progress(step_id='shutter_wait',
            progress_action='shutter_up' if session.active_direction == 'up' else 'shutter_down',
            progress_task=task)

    async def async_step_shutter_position_up(self, user_input=None):
        if self._shutter_session.direction != 'down':
            return await self._shutter_error()
        return await self.async_step_shutter_position_motor()

    async def async_step_shutter_position_down(self, user_input=None):
        if self._shutter_session.direction != 'up':
            return await self._shutter_error()
        return await self.async_step_shutter_position_motor()

    async def async_step_shutter_position_motor(self, user_input=None):
        try:
            await self._shutter_session.run(measuring=False)
        except HomeAssistantError:
            return await self._shutter_error()
        return await self.async_step_shutter_wait()

    async def async_step_shutter_edge_ready(self, user_input=None):
        self._shutter_edge_task = self.hass.async_create_task(
            self._shutter_session.edge_ready(), 'grenton shutter position check')
        return await self.async_step_shutter_check_position()

    async def async_step_shutter_check_position(self, user_input=None):
        task = self._shutter_edge_task
        if task.done():
            try:
                task.result()
            except HomeAssistantError:
                return self.async_show_progress_done(next_step_id='shutter_error')
            return self.async_show_progress_done(next_step_id='shutter_ready')
        return self.async_show_progress(step_id='shutter_check_position',
            progress_action='shutter_check_position', progress_task=task)

    async def async_step_shutter_ready(self, user_input=None):
        if self._shutter_session.finished:
            return self.async_abort(reason='calibration_expired')
        step = 'shutter_ready_tilt' if self._shutter_session.tilt_enabled else 'shutter_ready'
        if self._shutter_session.tilt_enabled and self._shutter_session.direction == 'up':
            step = 'shutter_ready_tilt_up'
        return self._shutter_menu(step, ['shutter_start_measurement', 'shutter_cancel'])

    async_step_shutter_ready_tilt = async_step_shutter_ready
    async_step_shutter_ready_tilt_up = async_step_shutter_ready

    async def async_step_shutter_start_measurement(self, user_input=None):
        try:
            await self._shutter_session.measure()
        except HomeAssistantError:
            return await self._shutter_error()
        return await self.async_step_shutter_measurement()

    async def async_step_shutter_measurement(self, user_input=None):
        session = self._shutter_session
        if session.finished:
            return self.async_abort(reason='calibration_expired')
        if user_input is not None:
            if user_input.get('measurement') == 'cancel':
                return await self.async_step_shutter_cancel()
            if user_input.get('measurement') in ('tilt_started', 'tilt_finished'):
                try:
                    if user_input['measurement'] == 'tilt_finished':
                        await session.mark_tilt_finished()
                    else:
                        await session.mark_tilt_started()
                except (vol.Invalid, HomeAssistantError):
                    return await self._shutter_error()
                return await self.async_step_shutter_measurement()
            return await self.async_step_shutter_stop_measurement()
        step, action, segment = 'shutter_measurement', 'finish', None
        if session.tilt_enabled:
            if session.direction == 'up':
                step = 'shutter_measurement_opening_travel' if session.phase == 'travelling' else 'shutter_measurement_opening_tilt'
                action = 'finish' if session.phase == 'travelling' else 'tilt_finished'
                segment = 'travel' if session.phase == 'travelling' else 'tilt'
            else:
                step = 'shutter_measurement_tilt_end' if session.phase == 'tilting' else 'shutter_measurement_tilt_start'
                action = 'finish' if session.phase == 'tilting' else 'tilt_started'
                segment = 'tilt' if session.phase == 'tilting' else 'travel'
        return self.async_show_form(step_id=step, data_schema=vol.Schema({
            vol.Required('measurement', default=action): ShutterMeasurementSelector({
                'flow_id': self.flow_id,
                'direction': session.direction,
                'elapsed_ms': round(session.elapsed_seconds * 1000),
                'segment': segment,
                'segment_started_ms': session.phase_change_ms or 0,
            }),
        }))

    async_step_shutter_measurement_tilt_start = async_step_shutter_measurement
    async_step_shutter_measurement_tilt_end = async_step_shutter_measurement
    async_step_shutter_measurement_opening_tilt = async_step_shutter_measurement
    async_step_shutter_measurement_opening_travel = async_step_shutter_measurement

    async def async_step_shutter_stop_measurement(self, user_input=None):
        try:
            await self._shutter_session.stop_measurement()
        except (vol.Invalid, HomeAssistantError):
            return await self._shutter_error()
        return await self.async_step_shutter_review()

    async def async_step_shutter_review(self, user_input=None):
        session = self._shutter_session
        entity = self._shutter()
        if session.phase != 'review' or session.elapsed_ms is None:
            return self.async_abort(reason='calibration_expired')
        errors = {}
        if user_input is not None:
            try:
                if session.tilt_enabled:
                    timings = ShutterTimingsSelector.validate(user_input.get('timings'))
                    value, lamel = timings['travel_ms'], timings['lamel_move_timeout']
                else:
                    value, lamel = milliseconds(user_input.get('total_ms')), 0
                if value <= 0:
                    raise vol.Invalid('Enter a positive movement time')
                settings = {session.key: value}
                if session.tilt_enabled:
                    settings['lamel_move_timeout'] = lamel
                async with session._lock:
                    adjustment = self._shutter_opposite_adjustment(timings) if session.tilt_enabled else None
                    await entity.apply_native_settings(settings, calibration=session, opposite_adjustment=adjustment)
                    self._shutter_pending_opposite = None
                    if session.tilt_enabled:
                        self._shutter_keep_total = timings['keep_total']
                    session._temporary = False
                    session.finished = True
                    session.phase = 'saved'
            except (vol.Invalid, HomeAssistantError):
                errors['base'] = 'cannot_apply_configuration'
            else:
                if session.tilt_enabled:
                    return await self.async_step_shutter_calibrated()
                return self.async_create_entry(title='', data=entity._merge_config_into_options(entity._config))
        configured = await self._shutter_configured_times()
        if configured is None:
            errors.setdefault('base', 'cannot_read_configuration')
        travel_default = (session.suggested_ms + 99) // 100 * 100
        if session.tilt_enabled:
            lamel_default = (session.lamel_ms + 99) // 100 * 100
            fields = self._shutter_timing_fields(user_input, travel_default, lamel_default, configured)
            travel = travel_default
        else:
            travel = (user_input or {}).get('total_ms', travel_default)
            fields = {vol.Required('total_ms', default=travel): number_selector()}
        return self.async_show_form(step_id='shutter_tilt_review' if session.tilt_enabled else 'shutter_review',
            last_step=not session.tilt_enabled, data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={'measured_ms': str(session.elapsed_ms), 'travel_ms': str(session.suggested_ms),
                                      'lamel_ms': str(session.lamel_ms), 'suggested_ms': str(travel),
                                      **{key: str((configured or {}).get(key)) if (configured or {}).get(key) is not None else '—'
                                         for key in ('configured_up_ms', 'configured_down_ms')}})

    async_step_shutter_tilt_review = async_step_shutter_review

    def _shutter_opposite_adjustment(self, timings):
        if not timings['adapt_opposite']:
            return None
        adjustment = getattr(self, '_shutter_pending_opposite', None)
        if adjustment is None:
            adjustment = OppositeTimingAdjustment(self._shutter_session.direction)
            self._shutter_pending_opposite = adjustment
        return adjustment

    async def _shutter_configured_times(self):
        entity, session = self._shutter(), self._shutter_session
        try:
            await entity.async_prepare_configuration()
        except HomeAssistantError:
            return None
        values = entity.setting_values
        if session._temporary:
            # Show the saved setting, rather than the provisional calibration limit.
            values[session.key] = session.original
        return {
            'configured_up_ms': values.get('blinds_up_max_time'),
            'configured_down_ms': values.get('blinds_down_max_time'),
            'configured_lamel_ms': values.get('lamel_move_timeout') if session.tilt_enabled else None,
        }

    def _shutter_timing_fields(self, user_input, travel, lamel, configured):
        session = self._shutter_session
        default = {'total_ms': travel + lamel, 'travel_ms': travel, 'lamel_move_timeout': lamel,
                   'keep_total': getattr(self, '_shutter_keep_total', True), 'adapt_opposite': False}
        return {vol.Required('timings', default=(user_input or {}).get('timings', default)):
                ShutterTimingsSelector({
                    'direction': session.direction,
                    'measured_ms': session.elapsed_ms,
                    'measured_travel_ms': session.suggested_ms if session.elapsed_ms is not None else None,
                    'measured_lamel_ms': session.lamel_ms if session.elapsed_ms is not None else None,
                    **{key: (configured or {}).get(key)
                       for key in ('configured_up_ms', 'configured_down_ms', 'configured_lamel_ms')},
                })}

    def _shutter_tilt_test_available(self):
        session = self._shutter_session
        return session.phase == 'saved' and session.tilt_enabled and self._shutter()._tilt_enabled

    async def async_step_shutter_calibrated(self, user_input=None):
        choices = ['shutter_done']
        if self._shutter_tilt_test_available():
            choices = ['shutter_tilt_test', 'shutter_tilt_adjust', *choices]
        return self._shutter_menu('shutter_calibrated', choices)

    async def async_step_shutter_done(self, user_input=None):
        entity = self._shutter()
        if self._shutter_session.phase != 'saved':
            return self.async_abort(reason='calibration_expired')
        return self.async_create_entry(title='', data=entity._merge_config_into_options(entity._config))

    async def _shutter_prepare_tilt_test(self, next_step):
        if not self._shutter_tilt_test_available():
            raise HomeAssistantError('Slat control is not enabled')
        session = self._shutter_session
        if session.running:
            self._shutter_next_after_wait = next_step
            return await self.async_step_shutter_wait()
        if not await session.refresh() or self._shutter()._number(self._shutter().cover_state) != 0:
            raise HomeAssistantError('Wait for the shutter to stop')
        return None

    async def async_step_shutter_tilt_test(self, user_input=None):
        if not self._shutter_tilt_test_available():
            return self.async_abort(reason='calibration_unavailable')
        errors = {}
        entity = self._shutter()
        try:
            if result := await self._shutter_prepare_tilt_test('shutter_tilt_test'):
                return result
            if user_input is not None:
                await entity.async_set_lamel_position(user_input.get('lamel_position'))
                return await self.async_step_shutter_calibrated()
        except HomeAssistantError:
            errors['base'] = 'cannot_test_tilt'
        angle = entity._number(entity.cover_tilt_position)
        angle = round(angle) if angle is not None and 0 <= angle <= 90 else 0
        return self.async_show_form(step_id='shutter_tilt_test', errors=errors, data_schema=vol.Schema({
            vol.Required('lamel_position', default=(user_input or {}).get('lamel_position', angle)):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0, max=90, step=1, unit_of_measurement='°', mode=selector.NumberSelectorMode.SLIDER)),
        }))

    async def async_step_shutter_tilt_adjust(self, user_input=None):
        if not self._shutter_tilt_test_available():
            return self.async_abort(reason='calibration_unavailable')
        errors = {}
        entity, session = self._shutter(), self._shutter_session
        try:
            if result := await self._shutter_prepare_tilt_test('shutter_tilt_adjust'):
                return result
            if user_input is not None:
                timings = ShutterTimingsSelector.validate(user_input.get('timings'))
                settings = {session.key: timings['travel_ms'], 'lamel_move_timeout': timings['lamel_move_timeout']}
                await entity.apply_native_settings(settings, opposite_adjustment=self._shutter_opposite_adjustment(timings))
                self._shutter_pending_opposite = None
                self._shutter_keep_total = timings['keep_total']
                return await self.async_step_shutter_calibrated()
        except (vol.Invalid, HomeAssistantError):
            errors['base'] = 'cannot_apply_configuration'
        configured = await self._shutter_configured_times()
        if configured is None:
            errors.setdefault('base', 'cannot_read_configuration')
        fields = self._shutter_timing_fields(user_input,
            entity.setting_values.get(session.key, 0), entity.setting_values.get('lamel_move_timeout', 0), configured)
        return self.async_show_form(step_id='shutter_tilt_adjust', errors=errors, data_schema=vol.Schema(fields))

    async def async_step_shutter_cancel(self, user_input=None):
        try:
            await self._shutter_session.abort()
        except HomeAssistantError:
            return await self._shutter_error()
        return self.async_abort(reason='calibration_cancelled')

    async def _shutter_error(self):
        return self._shutter_menu('shutter_error', ['shutter_cancel'],
                                  detail='Could not confirm shutter state.')
