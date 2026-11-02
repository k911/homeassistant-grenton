"""Stream CLU diagnostics using Object Manager's observed Telnet behavior."""

import asyncio
import codecs
import sys
from contextlib import suppress

CONNECT_TIMEOUT = 5
KEEPALIVE_INTERVAL = 5
INITIAL_NEGOTIATION = b"\xff\xfb\x00"  # IAC WILL BINARY


class LogOutputClosed(Exception):
    """The log consumer closed stdout; stop rather than reconnecting the CLU."""


def _emit(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError as error:
        raise LogOutputClosed from error


class TelnetDecoder:
    """Remove Telnet commands across reads and negotiate supported options.

    Match Object Manager: offer local BINARY, reject remote BINARY/ECHO/SGA
    and local SGA. Unknown options are refused. Subnegotiations are discarded.
    """

    def __init__(self) -> None:
        self._state = "data"
        self._command = 0

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        """Return application bytes and any required negotiation replies."""
        text = bytearray()
        replies = bytearray()
        for value in data:
            if self._state == "data":
                if value == 255:
                    self._state = "iac"
                else:
                    text.append(value)
            elif self._state == "iac":
                if value == 255:
                    text.append(value)
                    self._state = "data"
                elif value in (251, 252, 253, 254):
                    self._command = value
                    self._state = "option"
                elif value == 250:  # SB ... IAC SE
                    self._state = "subnegotiation"
                else:
                    self._state = "data"
            elif self._state == "option":
                if self._command == 251:  # WILL -> DONT
                    replies.extend((255, 254, value))
                elif self._command == 253 and value != 0:  # DO -> WONT
                    replies.extend((255, 252, value))
                # DO BINARY acknowledges our initial WILL; negatives need no reply.
                self._state = "data"
            elif self._state == "subnegotiation":
                if value == 255:
                    self._state = "subnegotiation_iac"
            elif self._state == "subnegotiation_iac":
                self._state = "data" if value == 240 else "subnegotiation"
        return bytes(text), bytes(replies)


async def stream_logs(host: str, port: int = 23) -> None:
    """Stream until EOF or failure; always close the socket and worker tasks."""
    async with asyncio.timeout(CONNECT_TIMEOUT):
        reader, writer = await asyncio.open_connection(host, port)

    async def receive() -> None:
        telnet = TelnetDecoder()
        utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while data := await reader.read(4096):
            payload, replies = telnet.feed(data)
            if replies:
                writer.write(replies)
                await writer.drain()
            if payload:
                _emit(utf8.decode(payload))
        _emit(utf8.decode(b"", final=True))

    async def keepalive() -> None:
        while True:
            writer.write(b"\r\n")
            await writer.drain()
            await asyncio.sleep(KEEPALIVE_INTERVAL)

    tasks: list[asyncio.Task] = []
    try:
        writer.write(INITIAL_NEGOTIATION)
        await writer.drain()
        tasks = [asyncio.create_task(receive()), asyncio.create_task(keepalive())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        with suppress(OSError, TimeoutError):
            async with asyncio.timeout(CONNECT_TIMEOUT):
                await writer.wait_closed()
