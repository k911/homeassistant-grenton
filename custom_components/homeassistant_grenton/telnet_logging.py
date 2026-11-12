"""Opt-in CLU Telnet sessions and forwarding to Home Assistant logging."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from homeassistant.exceptions import HomeAssistantError

from .domain.api.telnet import TelnetSessionRejected, stream_logs
from .domain.clu import GrentonClu
from .domain.device_types import attribute_index

_LOGGER = logging.getLogger(__name__)
LOG_LOGGER_NAME = f"{__package__}.clu_logs"
_CLU_LOGGER = logging.getLogger(LOG_LOGGER_NAME)
MAX_LINE_LENGTH = 4096
_SEVERITY = re.compile(
    r"^(?:(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+)?"
    r"\[(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\](?:\s|$)"
)
_LOG_USERS: set[object] = set()
_PREVIOUS_LOG_LEVEL = logging.NOTSET


def _enable_logger(owner: object) -> None:
    """Show all native levels only while at least one session owns the logger."""
    global _PREVIOUS_LOG_LEVEL
    if not _LOG_USERS:
        _PREVIOUS_LOG_LEVEL = _CLU_LOGGER.level
        _CLU_LOGGER.setLevel(logging.DEBUG)
    _LOG_USERS.add(owner)


def _release_logger(owner: object) -> None:
    _LOG_USERS.discard(owner)
    if not _LOG_USERS and _CLU_LOGGER.level == logging.DEBUG:
        _CLU_LOGGER.setLevel(_PREVIOUS_LOG_LEVEL)


class CluLogLines:
    """Reassemble UTF-8 text chunks with bounded memory and one record per line."""

    def __init__(self, clu: GrentonClu) -> None:
        self.clu = clu
        self.pending = ""

    def feed(self, text: str) -> None:
        for character in text:
            if character in "\r\n":
                self.flush()
            elif character == "\t" or character.isprintable():
                self.pending += character
                if len(self.pending) >= MAX_LINE_LENGTH:
                    self.flush()

    def flush(self) -> None:
        if line := self.pending:
            self.pending = ""
            match = _SEVERITY.match(line)
            level = getattr(logging, match[1]) if match else logging.INFO
            _CLU_LOGGER.log(level, "[%s / %s] %s", self.clu.name, self.clu.id, line)


class CluTelnetLogging:
    """A switch is on only for a negotiated session; failures never retry forever."""

    def __init__(
        self,
        clus: list[GrentonClu],
        enable_debug: Callable[[str], Awaitable[None]],
        notify: Callable[[str], None],
    ) -> None:
        self.clus = {clu.id: clu for clu in clus}
        self.enable_debug = enable_debug
        self.notify = notify
        self._locks = {clu.id: asyncio.Lock() for clu in clus}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active: set[str] = set()
        self._stopping = False

    def is_on(self, clu_id: str) -> bool:
        return clu_id in self._active

    async def async_start(self, clu_id: str) -> None:
        async with self._locks[clu_id]:
            if self._stopping:
                raise HomeAssistantError("Grenton is shutting down")
            if self.is_on(clu_id):
                return
            clu = self.clus[clu_id]
            if attribute_index(clu.device_type, "TelnetLogLevel") is None:
                raise HomeAssistantError(
                    f"CLU {clu.name} does not support Telnet logging"
                )
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(self._listen(clu, ready))
            self._tasks[clu_id] = task

            def finished(done: asyncio.Task) -> None:
                if self._tasks.get(clu_id) is done:
                    self._tasks.pop(clu_id)
                # A task cancelled before its first step never runs its finally block.
                if not ready.done():
                    if done.cancelled():
                        ready.cancel()
                    else:
                        ready.set_exception(
                            done.exception()
                            or TelnetSessionRejected("session ended during startup")
                        )

            task.add_done_callback(finished)
            try:
                await asyncio.shield(ready)
                if not self.is_on(clu_id):
                    raise TelnetSessionRejected("connection closed during startup")
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                # Retrieve a possible startup failure even if the caller was cancelled.
                if ready.done() and not ready.cancelled():
                    ready.exception()
                raise
            except Exception as error:
                await asyncio.gather(task, return_exceptions=True)
                detail = str(error) or type(error).__name__
                message = f"Could not start Telnet logging for {clu.name}: {detail}"
                if isinstance(error, (OSError, TimeoutError)):
                    message += (
                        ". Only one Telnet connection per CLU is supported; stop logging "
                        "in Object Manager or close the other Telnet client and try again."
                    )
                _LOGGER.warning(message)
                raise HomeAssistantError(message) from error

    async def _listen(self, clu: GrentonClu, ready: asyncio.Future) -> None:
        lines = CluLogLines(clu)
        owner = object()
        connected = False

        def accepted() -> None:
            nonlocal connected
            connected = True
            _enable_logger(owner)
            self._active.add(clu.id)
            ready.set_result(None)
            self.notify(clu.id)

        try:
            # Match the CLI: native verbosity must be enabled before connecting.
            await self.enable_debug(clu.id)
            await stream_logs(clu.ip, emit=lines.feed, on_connected=accepted)
            raise ConnectionError("CLU closed the Telnet stream")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - surface startup errors and contain background failures
            if not ready.done():
                ready.set_exception(error)
            elif not self._stopping:
                _LOGGER.warning(
                    "[%s] Telnet logging stopped: %s. The switch is now off; "
                    "enable it again to reconnect.",
                    clu.name,
                    error,
                )
        finally:
            lines.flush()
            if connected:
                _release_logger(owner)
            self._active.discard(clu.id)
            if not ready.done():
                ready.cancel()
            self.notify(clu.id)

    async def async_stop(self, clu_id: str) -> None:
        if task := self._tasks.get(clu_id):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def async_shutdown(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
