"""Read diagnostic and firmware inventory exports through the CLU management API."""

import asyncio
import json
import secrets

from clu_tftp import read_tftp
from homeassistant_grenton.domain.cipher import GrentonCipher


class ManagementProtocol(asyncio.DatagramProtocol):
    """Receive only the selected CLU's management responses as raw bytes."""

    def __init__(self, cipher: GrentonCipher, peer: tuple[str, int]):
        self.cipher = cipher
        self.peer = peer
        self.transport = None
        self.responses: asyncio.Queue[bytes | Exception] = asyncio.Queue(maxsize=64)

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr != self.peer:
            return
        decrypted = self.cipher.decrypt(data)
        if decrypted is not None and not self.responses.full():
            self.responses.put_nowait(decrypted)

    def error_received(self, exc: Exception) -> None:
        if not self.responses.full():
            self.responses.put_nowait(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self.error_received(exc or ConnectionError("CLU management socket closed"))


async def fetch_report(clu, encryption, kind: str, *, timeout: float = 10) -> dict:
    """Refresh an export and download its JSON without changing firmware/source."""
    if kind not in ("diagnostics", "firmware") or not 0 < timeout <= 300:
        raise ValueError("Select diagnostics or firmware and a timeout in (0, 300]")
    cipher = GrentonCipher(encryption)
    peer = (clu.ip, clu.port)
    protocol = ManagementProtocol(cipher, peer)
    transport = None

    async def request(payload: bytes, accepts) -> bytes:
        # Every operation has its own deadline; unrelated messages cannot extend it.
        encrypted = cipher.encrypt(payload)
        if encrypted is None:
            raise ValueError("Could not encrypt the CLU management request")
        transport.sendto(encrypted)
        try:
            async with asyncio.timeout(timeout):
                while True:
                    response = await protocol.responses.get()
                    if isinstance(response, Exception):
                        raise response
                    if accepts(response):
                        return response
        except TimeoutError as error:
            operation = payload.split(b":", 1)[0].decode("ascii")
            raise TimeoutError(
                f"CLU {operation} timed out after {timeout:g}s"
            ) from error

    try:
        async with asyncio.timeout(timeout):
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: protocol, remote_addr=peer,
            )
        if kind == "diagnostics":
            message_id = secrets.token_hex(4).encode("ascii")
            prefix = b"resp_diagnostic_refresh:" + message_id + b":"
            response = await request(
                b"req_diagnostic_refresh:" + clu.ip.encode("ascii") + b":" + message_id,
                lambda response: response.startswith(prefix),
            )
            if response != prefix + b"SUCCESS":
                raise ValueError("CLU diagnostic refresh was unsuccessful")
            path = "m:\\DIAGNOSTIC_PACK.JSON"
        else:
            prefix = b"resp_refresh_config:" + clu.ip.encode("ascii") + b":00000000:"
            # Captured response has binary f8 ff after this header. Do not decode
            # it as UTF-8 or assign unproven success/error semantics to those bytes.
            await request(b"req_refresh_config", lambda response: response.startswith(prefix))
            path = "a:\\CONFIG.JSON"
        await request(b"req_start_ftp", lambda response: response == b"resp:OK")
    finally:
        owned = transport if transport is not None else protocol.transport
        if owned is not None:
            owned.close()

    payload = await read_tftp(clu.ip, path, timeout=min(timeout, 3))
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("CLU export is not valid JSON") from error
    if not isinstance(result, dict):
        raise ValueError("CLU export must be a JSON object")  # noqa: TRY004 - invalid wire data
    arrays = (
        ("tfbusDiagnostic", "zwaveDiagnostic")
        if kind == "diagnostics" else ("tfbusDevices", "zwaveDevices")
    )
    if not isinstance(result.get("fwVer"), str) or any(
        not isinstance(result.get(key), list)
        or any(not isinstance(device, dict) for device in result[key])
        for key in arrays
    ):
        raise ValueError("CLU export is missing firmware or device inventory fields")
    if str(result.get("sn")) != str(clu.serial_number):
        raise ValueError("CLU export serial does not match the selected controller")
    return result
