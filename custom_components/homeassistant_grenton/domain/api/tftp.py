"""Read CLU diagnostic files through its fixed-port TFTP service."""

import asyncio
import math
import struct

TRANSFER_TIMEOUT = 60.0
BLOCK_SIZE = 512


class TftpError(OSError):
    """The CLU rejected a transfer or returned malformed TFTP data."""


class _TftpProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport = None
        self.peer = None
        self.messages: asyncio.Queue[bytes | Exception] = asyncio.Queue(maxsize=32)

    def connection_made(self, transport) -> None:
        self.transport = transport
        self.peer = transport.get_extra_info("peername")

    def datagram_received(self, data: bytes, addr) -> None:
        if addr == self.peer and not self.messages.full():
            self.messages.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        if not self.messages.full():
            self.messages.put_nowait(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self.error_received(exc or ConnectionError("TFTP connection closed"))


async def read_tftp(
    host: str,
    path: str,
    *,
    timeout: float = 3.0,
    retries: int = 3,
    max_bytes: int = 4 * 1024 * 1024,
) -> bytes:
    """Download a file without changing its wire bytes, including CRLF.

    This deliberately follows the captured CLU variant: all traffic uses server
    UDP port 69 and RRQ requests netascii. It is not a general-purpose TFTP client.
    Timeouts retransmit the most recent RRQ/ACK, with a retry budget per expected
    block and a 60-second deadline for the complete transfer.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("TFTP timeout must be a positive finite number")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("TFTP retries must be a non-negative integer")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("TFTP maximum size must be a non-negative integer")
    try:
        name = path.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("CLU TFTP paths must be ASCII") from error
    if not name or b"\0" in name:
        raise ValueError("Invalid CLU TFTP path")

    protocol = _TftpProtocol()
    try:
        async with asyncio.timeout(TRANSFER_TIMEOUT):
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: protocol, remote_addr=(host, 69),
            )
            outbound = b"\x00\x01" + name + b"\0netascii\0"
            expected = 1
            payload = bytearray()
            loop = asyncio.get_running_loop()
            transport.sendto(outbound)
            deadline = loop.time() + timeout
            attempts = 0
            while True:
                try:
                    async with asyncio.timeout_at(deadline):
                        message = await protocol.messages.get()
                except TimeoutError as error:
                    if attempts >= retries:
                        raise TimeoutError(
                            f"TFTP timed out waiting for block {expected}"
                        ) from error
                    transport.sendto(outbound)
                    attempts += 1
                    deadline = loop.time() + timeout
                    continue
                if isinstance(message, Exception):
                    raise message
                if len(message) < 4:
                    raise TftpError("Truncated TFTP response")
                opcode, block = struct.unpack_from("!HH", message)
                if opcode == 5:
                    end = message.find(b"\0", 4)
                    if end < 0:
                        raise TftpError("Malformed TFTP error response")
                    detail = message[4:end].decode("utf-8", errors="replace")[:200]
                    raise TftpError(f"TFTP error {block}: {detail}")
                if opcode != 3:
                    raise TftpError(f"Unexpected TFTP opcode {opcode}")
                if len(message) > BLOCK_SIZE + 4:
                    raise TftpError("Oversized TFTP DATA block")
                if block != expected:
                    # ACK the most recent accepted block. Duplicates and future
                    # blocks never extend the deadline or grow the payload.
                    if expected > 1:
                        transport.sendto(outbound)
                    continue
                body = message[4:]
                if len(payload) + len(body) > max_bytes:
                    raise TftpError("TFTP file exceeds maximum size")
                payload.extend(body)
                outbound = struct.pack("!HH", 4, block)
                transport.sendto(outbound)
                if len(body) < BLOCK_SIZE:
                    return bytes(payload)
                if expected == 65535:
                    raise TftpError("TFTP block number limit exceeded")
                expected += 1
                attempts = 0
                deadline = loop.time() + timeout
    finally:
        if protocol.transport is not None:
            protocol.transport.close()
