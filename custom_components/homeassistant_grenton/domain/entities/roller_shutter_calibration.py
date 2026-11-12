"""Observe firmware-owned calibration; cancellation never sends Stop."""

import asyncio
from dataclasses import replace
from time import monotonic

from homeassistant.exceptions import HomeAssistantError

from ..action import GrentonActionMethod
from .roller_shutter_configuration import milliseconds


class ShutterCalibration:
    """Keep stopwatch results separate from the module's calibration state."""

    POLL_SECONDS = 1
    STABLE_SAMPLES = 2

    def __init__(self, entity, direction: str, limit_ms: int):
        if direction not in ('up', 'down'):
            raise ValueError('Choose up or down')
        self.entity, self.direction = entity, direction
        self.limit_ms, self.lamel_ms = milliseconds(limit_ms), 0
        self.tilt_enabled = entity._tilt_enabled
        if self.limit_ms <= 0:
            raise ValueError('Provide a positive movement time')
        if self.tilt_enabled and not all('lamel_move_timeout' in bindings
                                        for bindings in (entity.setting_actions, entity.setting_states)):
            raise HomeAssistantError('Slat movement time cannot be saved')
        self.key = f'blinds_{direction}_max_time'
        self.original = entity.setting_values.get(self.key)
        if self.original is None or self.key not in entity.setting_actions:
            raise HomeAssistantError('Travel time is unavailable')
        self.started = self.elapsed_ms = None
        self.phase_change_ms = None
        self.phase, self.running, self.finished = 'prepare', False, False
        self._guard = None
        self._temporary = self.cancelled = False
        self._lock = asyncio.Lock()
        self.error = None
        self.active_direction = direction
        self._saw_motion = False
        self._initial_position = None
        self._measuring = False

    @property
    def elapsed_seconds(self):
        return self.elapsed_ms / 1000 if self.elapsed_ms is not None else (
            monotonic() - self.started if self.started is not None else 0)

    @property
    def seconds(self):
        return str(int(self.elapsed_seconds))

    @property
    def edge_position(self):
        return 0 if self.direction == 'up' else 100

    @property
    def at_edge(self):
        return self.entity._number(self.entity.cover_state) == 0 and self.entity.current_cover_position == self.edge_position and self._relays_idle()

    def _relays_idle(self):
        return all(source is None or self.entity._number(source) == 0
                   for source in (self.entity.up_state, self.entity.down_state))

    async def refresh(self):
        sources = [self.entity.cover_state, self.entity.cover_position]
        sources += [s for s in (self.entity.up_state, self.entity.down_state, self.entity.reverse_position) if s is not None]
        return await self.entity.coordinator.async_refresh_clu_state(
            self.entity.cover_state.clu_id, required_states=sources)

    async def _restore(self):
        if self._temporary:
            await self.entity._write_setting(self.key, self.original)
            if not await self.entity.coordinator.async_refresh_clu_state(
                self.entity.cover_state.clu_id, required_states=[self.entity.setting_states[self.key]]
            ) or self.entity.setting_values.get(self.key) != self.original:
                raise HomeAssistantError('Could not confirm the restored travel time')
            self._temporary = False

    def _observe(self):
        self.error = None
        self._guard = asyncio.create_task(self._wait_idle())
        def completed(task):
            if not task.cancelled() and (error := task.exception()) is not None:
                self.error = str(error)
        self._guard.add_done_callback(completed)

    async def _wait_idle(self):
        stable = 0
        deadline = monotonic() + self.limit_ms / 1000 + 60
        while monotonic() < deadline:
            if getattr(self.entity.coordinator, '_stopping', False) is True:
                raise HomeAssistantError('Connection closed during calibration')
            fresh = await self.refresh()
            state, position = self.entity._number(self.entity.cover_state), self.entity.current_cover_position
            if fresh and (state in (1, 2, 4) or (position is not None and self._initial_position is not None and position != self._initial_position)):
                self._saw_motion = True
            target = 100 if self.active_direction == 'up' else 0
            if fresh and self._saw_motion and state == 0 and position == target and self._relays_idle():
                stable += 1
                if stable >= self.STABLE_SAMPLES:
                    async with self._lock:
                        self.running = False
                        await self._restore()
                        if self.cancelled:
                            self.phase, self.finished = 'cancelled', True
                        elif not self._measuring:
                            self.phase = 'ready'
                    return
            else:
                stable = 0
            await asyncio.sleep(self.POLL_SECONDS)
        raise HomeAssistantError('Could not confirm calibration completion')

    async def wait(self):
        if self.running and (self._guard is None or self._guard.done()):
            self._observe()
        if self._guard is not None:
            await asyncio.shield(self._guard)

    async def run(self, *, measuring):
        async with self._lock:
            if self.finished or self.running or (measuring and self.phase != 'ready'):
                raise HomeAssistantError('Wait for calibration to finish')
            if not await self.refresh() or self.entity._number(self.entity.cover_state) != 0:
                raise HomeAssistantError('Wait for the shutter to stop')
            if measuring and not self.at_edge:
                raise HomeAssistantError('Move the shutter to the opposite end first')
            if measuring:
                await self.entity.async_prepare_configuration()
                self.original = self.entity.setting_values[self.key]
                self._temporary = True
                await self.entity._write_setting(self.key, self.limit_ms)
                if not await self.entity.coordinator.async_refresh_clu_state(
                    self.entity.cover_state.clu_id, required_states=[self.entity.setting_states[self.key]]
                ) or self.entity.setting_values.get(self.key) != self.limit_ms:
                    raise HomeAssistantError('Could not confirm movement time')
            self._measuring = measuring
            self.active_direction = self.direction if measuring else ('down' if self.direction == 'up' else 'up')
            self.started, self.elapsed_ms = monotonic(), None
            self.phase_change_ms, self.lamel_ms = None, 0
            self._initial_position = self.entity.current_cover_position
            self._saw_motion = False
            self.phase = 'measuring' if measuring else 'positioning'
            self.running = True
            # Even a lost command reply must leave observation active.
            template = self.entity.calibration_action
            value = '1' if self.active_direction == 'up' else '0'
            try:
                if isinstance(template, GrentonActionMethod):
                    await self.entity._execute(replace(template, value=value, arguments=(value,)))
                else:
                    await self.entity._execute(template, value)
            finally:
                self._observe()

    async def edge_ready(self):
        for sample in range(self.STABLE_SAMPLES):
            if sample:
                await asyncio.sleep(self.POLL_SECONDS)
            if self.finished or self.running or not await self.refresh() or not self.at_edge:
                raise HomeAssistantError('Wait for the opposite end position')
        self.phase = 'ready'

    async def measure(self):
        await self.run(measuring=True)

    async def mark_tilt_started(self):
        """Closing: travel has finished and slat movement starts."""
        await self._mark_phase_change('down')

    async def mark_tilt_finished(self):
        """Opening: slat movement has finished and travel starts."""
        await self._mark_phase_change('up')

    async def _mark_phase_change(self, direction):
        async with self._lock:
            if (not self.tilt_enabled or self.direction != direction or self.phase != 'measuring'
                    or self.started is None or self.cancelled):
                raise HomeAssistantError('No slat measurement is running')
            elapsed = milliseconds(round((monotonic() - self.started) * 1000))
            if elapsed <= 0:
                raise HomeAssistantError('Movement time must be positive')
            self.phase_change_ms = elapsed
            self.phase = 'travelling' if direction == 'up' else 'tilting'

    async def stop_measurement(self):
        async with self._lock:
            expected_phase = ('travelling' if self.direction == 'up' else 'tilting') if self.tilt_enabled else 'measuring'
            if self.phase != expected_phase or self.started is None or self.cancelled:
                raise HomeAssistantError('No measurement is running')
            elapsed = milliseconds(round((monotonic() - self.started) * 1000), 1200000)
            if self.tilt_enabled:
                first_ms, second_ms = self.phase_change_ms, milliseconds(elapsed - self.phase_change_ms)
                if second_ms <= 0:
                    raise HomeAssistantError('Movement time must be positive')
                lamel_ms = first_ms if self.direction == 'up' else second_ms
                self.lamel_ms = lamel_ms
            else:
                milliseconds(elapsed)
            self.elapsed_ms = elapsed
            self.phase = 'review'

    @property
    def suggested_ms(self):
        if self.elapsed_ms is None or self.elapsed_ms <= self.lamel_ms:
            raise HomeAssistantError('Measured time must exceed slat time')
        return milliseconds(self.elapsed_ms - self.lamel_ms)

    async def abort(self):
        async with self._lock:
            if self.phase == 'saved':
                return
            self.cancelled, self.elapsed_ms = True, None
            self.phase_change_ms, self.lamel_ms = None, 0
            if self.running:
                self.phase = 'cancelling'
                if self._guard is None or self._guard.done():
                    self._observe()
            else:
                await self._restore()
                self.phase, self.finished = 'cancelled', True
