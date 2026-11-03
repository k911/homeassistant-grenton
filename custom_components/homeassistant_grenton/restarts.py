"""Uptime-based restart observations and optional bounded debug-file capture."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from .domain.api.clu_files import download_file
from .domain.device_types import attribute_index
from .state import GrentonCluStateAttributeKey

_LOGGER = logging.getLogger(__name__)
CONF_DEBUG_CAPTURE = "clu_debug_capture"
FETCH_ALLOWANCE = 5
CAPTURE_ATTEMPTS = 2
CAPTURE_RETRY_DELAY = 5
MAX_DUMPS = 5
MAX_DUMP_BYTES = 4 * 1024 * 1024


@dataclass
class RestartRecord:
    boot_time: datetime | None = None
    detected_at: datetime | None = None
    previous_uptime: int | None = None
    uptime: int | None = None
    capture_status: str = "disabled"
    capture_error: str | None = None
    capture_file: str | None = None


def _uptime(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = int(value)
    except (ValueError, OverflowError):
        return None
    if result < 0 or (isinstance(value, float) and result != value):
        return None
    return result


def _save_dump(directory: Path, name: str, payload: bytes) -> None:
    """Atomically publish a complete dump, then prune only our own older files."""
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            dir=directory, prefix=".debug-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
        try:
            for old in sorted(directory.glob("debug-*.bin"), reverse=True)[MAX_DUMPS:]:
                if old.is_file():
                    old.unlink()
        except OSError:
            # A failed rotation must not leave an extra newly published dump.
            (directory / name).unlink(missing_ok=True)
            raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CluRestartMonitor:
    """Keep baselines in memory; only the independent capture preference persists."""

    def __init__(self, hass, entry, clus, encryption, intervals, notify):
        self.hass = hass
        self.entry = entry
        self.clus = {clu.id: clu for clu in clus}
        self.encryption = encryption
        self.intervals = intervals
        self.notify = notify
        self.records = {clu.id: RestartRecord() for clu in clus}
        self.last_uptime: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._captures: dict[str, asyncio.Task] = {}
        self._stopping = False

    def capture_enabled(self, clu_id: str) -> bool:
        settings = self.entry.options.get(CONF_DEBUG_CAPTURE, {})
        return isinstance(settings, Mapping) and settings.get(clu_id) is True

    async def async_set_capture(self, clu_id: str, enabled: bool) -> None:
        settings = self.entry.options.get(CONF_DEBUG_CAPTURE, {})
        updated = dict(settings) if isinstance(settings, Mapping) else {}
        updated[clu_id] = enabled
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_DEBUG_CAPTURE: updated},
        )
        if not enabled:
            if task := self._captures.get(clu_id):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.records[clu_id].capture_status = "disabled"
        self.notify(clu_id)

    def observe(self, clu_id: str, keys, values) -> None:
        if self._stopping:
            return
        clu = self.clus[clu_id]
        index = attribute_index(clu.device_type, "Uptime")
        for key, value in zip(keys, values):
            if (
                isinstance(key, GrentonCluStateAttributeKey)
                and key.object_name == clu.object_name
                and key.name == index
                and (seconds := _uptime(value)) is not None
            ):
                self._observe_uptime(clu_id, seconds)

    def _observe_uptime(self, clu_id: str, seconds: int) -> None:
        previous = self.last_uptime.get(clu_id)
        self.last_uptime[clu_id] = seconds
        now = datetime.now(UTC)
        try:
            boot = now - timedelta(seconds=seconds)
        except OverflowError:
            # Invalid huge values must not poison the last good baseline.
            if previous is None:
                self.last_uptime.pop(clu_id, None)
            else:
                self.last_uptime[clu_id] = previous
            return
        record = self.records[clu_id]
        if previous is None:
            record.boot_time = boot
            if seconds > self.intervals[clu_id] + FETCH_ALLOWANCE:
                return
        elif seconds >= previous:
            return
        record = RestartRecord(
            boot_time=boot,
            detected_at=now,
            previous_uptime=previous,
            uptime=seconds,
            capture_status="pending" if self.capture_enabled(clu_id) else "disabled",
        )
        self.records[clu_id] = record
        clu = self.clus[clu_id]
        _LOGGER.warning(
            "[%s] %s detected from uptime: previous=%s, current=%ds; estimated boot=%s",
            clu.name,
            "Recent CLU restart" if previous is None else "CLU restart",
            previous,
            seconds,
            boot.isoformat(),
        )
        if self.capture_enabled(clu_id):
            previous_task = self._captures.get(clu_id)
            if previous_task is not None:
                previous_task.cancel()
            task = asyncio.create_task(self._capture(clu_id, record, previous_task))
            self._captures[clu_id] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _capture(self, clu_id: str, record: RestartRecord, previous_task) -> None:
        if previous_task is not None:
            await asyncio.gather(previous_task, return_exceptions=True)
        clu = self.clus[clu_id]
        for attempt in range(CAPTURE_ATTEMPTS):
            if self._stopping or not self.capture_enabled(clu_id):
                return
            try:
                payload = await download_file(
                    clu,
                    self.encryption,
                    "a:\\debug.bin",
                    timeout=5,
                    max_bytes=MAX_DUMP_BYTES,
                )
                if not payload:
                    raise ValueError("debug.bin is empty")
                entry = hashlib.sha256(self.entry.entry_id.encode()).hexdigest()[:16]
                controller = hashlib.sha256(clu.id.encode()).hexdigest()[:16]
                relative = Path("grenton") / "debug" / entry / controller
                name = f"debug-{record.detected_at.strftime('%Y%m%dT%H%M%S%fZ')}.bin"
                job = self.hass.async_add_executor_job(
                    _save_dump,
                    Path(self.hass.config.path(str(relative))),
                    name,
                    payload,
                )
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    # Executor writes cannot be cancelled; finish before shutdown/replacement.
                    await job
                    raise
                record.capture_status = "saved"
                record.capture_error = None
                record.capture_file = str(relative / name)
                _LOGGER.info(
                    "[%s] Captured debug.bin (%d bytes)", clu.name, len(payload)
                )
                self.notify(clu_id)
                return
            except (OSError, ValueError, TimeoutError) as error:
                record.capture_error = str(error) or type(error).__name__
                if attempt + 1 < CAPTURE_ATTEMPTS:
                    await asyncio.sleep(CAPTURE_RETRY_DELAY)
        record.capture_status = "failed"
        _LOGGER.warning(
            "[%s] Could not capture debug.bin after restart: both download attempts "
            "failed (%s). A debug file may not be available for this restart.",
            clu.name,
            record.capture_error,
        )
        self.notify(clu_id)

    async def async_shutdown(self) -> None:
        self._stopping = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._captures.clear()
