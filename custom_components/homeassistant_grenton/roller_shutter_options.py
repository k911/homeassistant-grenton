"""Native options-flow steps for user-directed shutter calibration."""

import asyncio
import logging
import voluptuous as vol
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .domain.entities.roller_shutter_configuration import milliseconds, number_selector


_LOGGER = logging.getLogger(__name__)


class RollerShutterOptionsMixin:
    """Calibration never starts from opening a dialog or submitting settings."""

    @callback
    def async_remove(self):
        """Discard an unfinished measurement while firmware continues moving."""
        super().async_remove()
        clock = getattr(self, '_shutter_clock', None)
        if clock is not None:
            clock.cancel()
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
            if session.phase == 'measuring':
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
                self._shutter_direction = direction
                return await self.async_step_shutter_limits()
            errors['direction'] = 'invalid_configuration'
        return self.async_show_form(step_id='shutter_calibration', data_schema=vol.Schema({
            vol.Required('direction', default='up'): selector.SelectSelector(selector.SelectSelectorConfig(
                options=['up', 'down'], translation_key='shutter_directions',
                mode=selector.SelectSelectorMode.DROPDOWN,
            )),
        }), errors=errors)

    async def async_step_shutter_limits(self, user_input=None):
        entity = self._shutter()
        errors = {}
        name = 'limit_up_ms' if self._shutter_direction == 'up' else 'limit_down_ms'
        if user_input is not None:
            try:
                limit = milliseconds(user_input.get(name, entity.travel_time(self._shutter_direction)))
                lamel = milliseconds(user_input.get('lamel_move_timeout', 0)) if entity._tilt_enabled else 0
                self._shutter_session = await entity.create_calibration(self._shutter_direction, limit, lamel)
            except (vol.Invalid, ValueError, HomeAssistantError):
                errors['base'] = 'invalid_configuration'
            else:
                return await self.async_step_shutter_position()
        fields = {vol.Optional(name, default=(user_input or {}).get(name, entity.travel_time(self._shutter_direction))): number_selector()}
        if entity._tilt_enabled:
            fields[vol.Required('lamel_move_timeout', default=(user_input or {}).get('lamel_move_timeout', entity.setting_values.get('lamel_move_timeout', 0)))] = number_selector()
        return self.async_show_form(step_id='shutter_limits', data_schema=vol.Schema(fields), errors=errors)

    async def async_step_shutter_position(self, user_input=None):
        session = self._shutter_session
        if session.running:
            return await self.async_step_shutter_wait()
        await session.refresh()
        button = 'shutter_position_down' if session.direction == 'up' else 'shutter_position_up'
        closed = session.direction == 'up'
        if session.at_edge:
            choices = ['shutter_edge_ready', 'shutter_cancel']
            step = 'shutter_position_closed' if closed else 'shutter_position_open'
        else:
            choices = [button, 'shutter_cancel']
            step = 'shutter_position_close_required' if closed else 'shutter_position_open_required'
        return self._shutter_menu(step, choices)

    async_step_shutter_position_closed = async_step_shutter_position
    async_step_shutter_position_open = async_step_shutter_position
    async_step_shutter_position_close_required = async_step_shutter_position
    async_step_shutter_position_open_required = async_step_shutter_position

    async def _shutter_refresh_clock(self, *, wait_for_idle):
        session = self._shutter_session
        if wait_for_idle:
            task = asyncio.create_task(session.wait())
        try:
            while not wait_for_idle or not task.done():
                await asyncio.sleep(1)
                self.async_notify_flow_changed()
            await task
        finally:
            if wait_for_idle and not task.done():
                task.cancel()  # session.wait shields the firmware observer.

    async def async_step_shutter_wait(self, user_input=None):
        session = self._shutter_session
        task = getattr(self, '_shutter_wait_task', None)
        if task is None:
            task = self.hass.async_create_task(self._shutter_refresh_clock(wait_for_idle=True), 'grenton shutter wait')
            self._shutter_wait_task = task
        if task.done():
            self._shutter_wait_task = None
            try:
                task.result()
            except HomeAssistantError:
                return self.async_show_progress_done(next_step_id='shutter_error')
            return self.async_show_progress_done(next_step_id='shutter_calibration' if session.finished else 'shutter_position')
        return self.async_show_progress(step_id='shutter_wait',
            progress_action='shutter_up' if session.active_direction == 'up' else 'shutter_down',
            progress_task=task, description_placeholders={'seconds':session.seconds})

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
        return self._shutter_menu('shutter_ready', ['shutter_start_measurement', 'shutter_cancel'])

    async def async_step_shutter_start_measurement(self, user_input=None):
        try:
            await self._shutter_session.measure()
        except HomeAssistantError:
            return await self._shutter_error()
        return await self.async_step_shutter_measurement()

    async def async_step_shutter_measurement(self, user_input=None):
        if self._shutter_session.finished:
            return self.async_abort(reason='calibration_expired')
        if getattr(self, '_shutter_clock', None) is None:
            self._shutter_clock = self.hass.async_create_task(self._shutter_refresh_clock(wait_for_idle=False), 'grenton shutter clock')
        icon = '↑' if self._shutter_session.direction == 'up' else '↓'
        return self._shutter_menu('shutter_measurement', ['shutter_stop_measurement', 'shutter_cancel'],
                                  icon=icon, seconds=self._shutter_session.seconds)

    async def async_step_shutter_stop_measurement(self, user_input=None):
        clock = getattr(self, '_shutter_clock', None)
        if clock is not None:
            clock.cancel()
            self._shutter_clock = None
        try:
            await self._shutter_session.stop_measurement()
        except HomeAssistantError:
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
                if user_input.get('save') is not True:
                    await session.abort()
                    return await self.async_step_shutter()
                total = milliseconds(user_input.get('total_ms'), maximum=610000)
                lamel = milliseconds(user_input.get('lamel_move_timeout', session.lamel_ms)) if session.tilt_enabled else 0
                if session.tilt_enabled and lamel <= 0:
                    raise vol.Invalid('Enter a positive slat movement time')
                value = milliseconds(total - lamel)
                if value <= 0:
                    raise vol.Invalid('Total must exceed slat time')
                settings = {session.key: value}
                if session.tilt_enabled:
                    settings['lamel_move_timeout'] = lamel
                async with session._lock:
                    await entity.apply_native_settings(settings, calibration=session)
                    session._temporary = False
                    session.finished = True
                    session.phase = 'saved'
            except (vol.Invalid, HomeAssistantError):
                errors['base'] = 'cannot_apply_configuration'
            else:
                return self.async_create_entry(title='', data=entity._merge_config_into_options(entity._config))
        total = (user_input or {}).get('total_ms', session.elapsed_ms)
        lamel = (user_input or {}).get('lamel_move_timeout', session.lamel_ms)
        fields = {vol.Required('total_ms', default=total): number_selector(610000)}
        if session.tilt_enabled:
            fields[vol.Required('lamel_move_timeout', default=lamel)] = number_selector()
        fields[vol.Required('save', default=False)] = selector.BooleanSelector()
        proposed = total - lamel if isinstance(total, (int, float)) and isinstance(lamel, (int, float)) else 0
        return self.async_show_form(step_id='shutter_review', last_step=True, data_schema=vol.Schema(fields), errors=errors,
                                    description_placeholders={'measured_ms': str(session.elapsed_ms), 'suggested_ms': str(proposed), 'direction': session.direction})

    async def async_step_shutter_cancel(self, user_input=None):
        try:
            await self._shutter_session.abort()
        except HomeAssistantError:
            return await self._shutter_error()
        return self.async_abort(reason='calibration_cancelled')

    async def _shutter_error(self):
        return self._shutter_menu('shutter_error', ['shutter_cancel'],
                                  detail='Could not confirm shutter state.')
