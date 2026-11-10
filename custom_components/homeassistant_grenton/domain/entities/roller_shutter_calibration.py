"""User-operated shutter stopwatch with bounded motor runs and readback."""

from dataclasses import replace
import asyncio
import logging
from time import monotonic

from homeassistant.exceptions import HomeAssistantError

from ..action import GrentonActionMethod
from .roller_shutter_configuration import milliseconds

_LOGGER = logging.getLogger(__name__)


class ShutterCalibration:
    """One in-memory calibration owner per cover, independent of popup lifetime."""

    MARGIN_MS = 10000

    def __init__(self, entity, direction: str, limit_ms: int, lamel_ms: int = 0):
        if direction not in ('up', 'down'):
            raise ValueError('Choose up or down')
        self.entity = entity
        self.direction = direction
        self.limit_ms = milliseconds(limit_ms)
        self.lamel_ms = milliseconds(lamel_ms)
        self.tilt_enabled = entity._tilt_enabled
        if self.tilt_enabled and self.lamel_ms <= 0:
            raise ValueError('Provide the manually measured slat movement time')
        if self.limit_ms <= 0:
            raise ValueError('Measurement time limit must be positive')
        self.key = f'blinds_{direction}_max_time'
        self.original = entity.setting_values.get(self.key)
        if self.original is None or self.key not in entity.setting_actions:
            raise HomeAssistantError('Travel-time binding is unavailable')
        self.started = None
        self.elapsed_ms = None
        self.phase = 'prepare'
        self.running = False
        self.finished = False
        self._guard = None
        self._temporary = False
        self._lock = asyncio.Lock()

    async def _restore(self):
        if self._temporary:
            await self.entity._write_setting(self.key, self.original)
            if not await self.entity.coordinator.async_refresh_clu_state(
                self.entity.cover_state.clu_id, required_states=[self.entity.setting_states[self.key]]
            ) or self.entity.setting_values.get(self.key) != self.original:
                raise HomeAssistantError('Could not confirm the restored travel-time limit')
            self._temporary = False

    async def _stop(self, *, force=False):
        # A failed reply cannot establish that the motor did not start.
        if self.running or force:
            await self.entity._execute(self.entity.stop)
            self.running = False
        await self._restore()

    def _cancel_guard(self):
        if self._guard is not None and self._guard is not asyncio.current_task():
            self._guard.cancel()
        self._guard = None

    async def _deadline(self):
        try:
            await asyncio.sleep((self.limit_ms + self.MARGIN_MS) / 1000)
            for attempt in range(3):
                async with self._lock:
                    self.phase = 'expired'
                    self.elapsed_ms = None
                    try:
                        await self._stop()
                    except HomeAssistantError:
                        _LOGGER.exception(
                            'Could not stop/restore shutter calibration for %s (attempt %s)',
                            self.entity.unique_id, attempt + 1,
                        )
                    else:
                        self.finished = True
                        return
                if attempt < 2:
                    await asyncio.sleep(2)
            self.finished = True
        except asyncio.CancelledError:
            return

    async def run(self, *, measuring: bool):
        async with self._lock:
            if self.finished:
                raise HomeAssistantError('Calibration has expired or was cancelled')
            if measuring and self.phase != 'ready':
                raise HomeAssistantError('Confirm the opposite end position first')
            if not measuring and self.phase not in ('prepare', 'positioning'):
                raise HomeAssistantError('Return to preparation before positioning')
            # Do not disarm the previous run's guard until Stop succeeds.
            await self._stop(force=True)
            self._cancel_guard()
            self.phase = 'measuring' if measuring else 'positioning'
            self.elapsed_ms = None
            try:
                if measuring:
                    await self.entity.async_prepare_configuration()
                    self.original = self.entity.setting_values[self.key]
                    # Mark this before the write: a lost ACK may still mean it applied.
                    self._temporary = True
                    await self.entity._write_setting(self.key, self.limit_ms)
                    if not await self.entity.coordinator.async_refresh_clu_state(
                        self.entity.cover_state.clu_id,
                        required_states=[self.entity.setting_states[self.key]],
                    ) or self.entity.setting_values.get(self.key) != self.limit_ms:
                        raise HomeAssistantError('Could not confirm the temporary measurement limit')
                direction = self.direction if measuring else (
                    'down' if self.direction == 'up' else 'up'
                )
                self.started = monotonic() if measuring else None
                self.running = True
                self._guard = asyncio.create_task(self._deadline())
                template = self.entity.calibration_action
                value = '1' if direction == 'up' else '0'
                if isinstance(template, GrentonActionMethod):
                    # Calibration takes a numeric direction, not a Lua boolean.
                    await self.entity._execute(replace(template, value=value, arguments=(value,)))
                else:
                    await self.entity._execute(template, value)
            except BaseException:
                self.phase = 'failed'
                self.elapsed_ms = None
                try:
                    await self._stop()
                except HomeAssistantError:
                    # Keep the guard alive if the stop itself fails.
                    _LOGGER.exception('Could not clean up failed calibration start')
                raise

    async def edge_ready(self):
        async with self._lock:
            if self.finished:
                raise HomeAssistantError('Calibration has expired or was cancelled')
            await self._stop(force=True)
            self._cancel_guard()
            self.phase = 'ready'

    async def measure(self):
        if self.phase != 'ready':
            raise HomeAssistantError('Confirm the opposite end position first')
        await self.run(measuring=True)

    async def stop_measurement(self):
        async with self._lock:
            if self.phase != 'measuring' or self.started is None or self.finished:
                raise HomeAssistantError('No measurement is running')
            elapsed = round((monotonic() - self.started) * 1000)
            if elapsed > self.limit_ms + self.MARGIN_MS:
                await self._stop()
                self._cancel_guard()
                self.phase = 'expired'
                self.finished = True
                raise HomeAssistantError('Measurement time limit exceeded')
            await self._stop()
            self._cancel_guard()
            self.elapsed_ms = elapsed
            self.phase = 'review'

    @property
    def suggested_ms(self):
        if self.elapsed_ms is None or self.elapsed_ms <= self.lamel_ms:
            raise HomeAssistantError('Measured total must exceed the slat movement time')
        return milliseconds(self.elapsed_ms - self.lamel_ms)

    async def abort(self):
        async with self._lock:
            # Retain the deadline if stopping fails, so it can try again.
            await self._stop()
            self._cancel_guard()
            self.phase = 'cancelled'
            self.finished = True
            self.elapsed_ms = None
