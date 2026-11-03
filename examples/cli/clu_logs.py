"""Print CLU diagnostics using the shared Telnet transport."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components"))

from homeassistant_grenton.domain.api.telnet import (
    INITIAL_NEGOTIATION,
    TelnetDecoder,
    TelnetSessionRejected,
)
from homeassistant_grenton.domain.api.telnet import stream_logs as _stream_logs

__all__ = [
    "INITIAL_NEGOTIATION",
    "LogOutputClosed",
    "TelnetDecoder",
    "TelnetSessionRejected",
    "stream_logs",
]


class LogOutputClosed(Exception):
    """The log consumer closed stdout; stop rather than reconnecting the CLU."""


def _emit(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError as error:
        raise LogOutputClosed from error


async def stream_logs(host: str, port: int = 23) -> None:
    await _stream_logs(host, port, emit=_emit)
