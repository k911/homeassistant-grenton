from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, cast

from ...state import (
    MISSING_STATE,
    GrentonCluStateAttributeKey,
    GrentonCluStateVariableKey,
    GrentonRegistrationValue,
    GrentonValue,
)
from ..action import GrentonAction
from ..cipher import GrentonCipher
from ..clu import GrentonClu
from ..encryption import GrentonEncryption
from .clu_messages import (
    GrentonCluApiActionRequest,
    GrentonCluApiClientRegisterRequest,
    GrentonCluApiClientRegisterResponse,
    GrentonCluApiClientReportNotification,
    GrentonCluApiMessageParser,
    GrentonCluApiPingRequest,
)

_LOGGER = logging.getLogger(__name__)

# CLU's UDP buffer caps how many keys fit in one clientRegister payload.
# 30 keys is empirically safe; payload + AES padding stays under the MTU.
MAX_KEYS_PER_REGISTER = 30

StateKey = GrentonCluStateVariableKey | GrentonCluStateAttributeKey


@dataclass
class _SubscriptionEndpoint:
    """One UDP socket dedicated to a single sub-subscription (chunk of keys).

    Each chunk gets its own socket because CLU client_report notifications do
    not carry a usable session_id — the only way to know which chunk a report
    belongs to is to receive it on a chunk-specific socket.
    """
    transport: Optional[asyncio.DatagramTransport]
    protocol: Optional["GrentonCluApiProtocol"]
    keys: list[StateKey]
    session_id: int
    keep_alive_id: str = field(default_factory=lambda: secrets.token_hex(4))
    registration_id: str = field(default_factory=lambda: secrets.token_hex(4))
    ready: bool = True


class GrentonCluApi:
    """Handles all communication with Grenton CLUs via UDP."""

    def __init__(self, clu: GrentonClu, encryption: GrentonEncryption):
        self.clu = clu
        self.encryption = encryption
        self.cipher = GrentonCipher(encryption)

        # Serialize outgoing actions per-CLU so grouped commands don't burst
        # the shared UDP socket (CLU drops datagrams under a burst).
        self._action_lock = asyncio.Lock()
        # All sockets share one controller: space packets without serializing
        # their response waits, including during startup and keepalives.
        self._send_lock = asyncio.Lock()
        # Readbacks can overlap the periodic refresh. Stable registration IDs
        # require one in-flight registration per subscription socket.
        self._registration_lock = asyncio.Lock()
        self.large_value_keys: set[StateKey] = set()
        self._closed = False

        # Main socket — used for pings and actions only.
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional[GrentonCluApiProtocol] = None
        self.keep_alive_id = secrets.token_hex(4)

        # One socket per chunk of subscribed keys. Reports arrive on the same
        # socket that registered them, so mapping is implicit.
        self._subscription_endpoints: list[_SubscriptionEndpoint] = []

        # Set by the coordinator; invoked with (keys, values) for every report.
        self.on_subscription_report: Optional[
            Callable[[list[StateKey], list[GrentonValue]], Awaitable[None]]
        ] = None

    async def connect(self) -> bool:
        """Open the main UDP socket used for pings and actions."""
        if self.transport is not None:
            return True
        self._closed = False
        try:
            loop = asyncio.get_event_loop()

            protocol = GrentonCluApiProtocol(self)
            self.protocol = protocol

            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=('0.0.0.0', 0),
            )
            if self._closed:
                transport.close()
                self.protocol = None
                return False
            self.transport = transport

            _LOGGER.debug("[%s] Opened main UDP socket to %s:%d", self.clu.id, self.clu.ip, self.clu.port)
            return True

        except Exception as e:
            _LOGGER.error("[%s] Failed to create main UDP socket to %s:%d: %s",
                          self.clu.id, self.clu.ip, self.clu.port, e)
            return False

    async def disconnect(self) -> None:
        """Close all sockets — main and per-subscription."""
        self._closed = True
        self.on_subscription_report = None
        await self._close_subscription_endpoints()

        if self.transport:
            self.transport.close()
            _LOGGER.debug("[%s] Closed main UDP socket", self.clu.id)
            self.transport = None
        self.protocol = None

    async def _close_subscription_endpoints(self) -> None:
        for endpoint in self._subscription_endpoints:
            try:
                if endpoint.transport:
                    endpoint.transport.close()
            except Exception as e:
                _LOGGER.debug("[%s] Error closing subscription socket sid=%d: %s",
                              self.clu.id, endpoint.session_id, e)
        if self._subscription_endpoints:
            _LOGGER.debug("[%s] Closed %d subscription socket(s)",
                          self.clu.id, len(self._subscription_endpoints))
        self._subscription_endpoints = []

    async def ping(self) -> bool:
        """Keep the action and every subscription UDP endpoint alive."""
        if self._closed or not self.protocol:
            _LOGGER.warning("[%s] No protocol available for ping", self.clu.id)
            return False

        # A CLU tracks client liveness by source UDP endpoint. Pinging only the
        # main/action socket does not keep the separate subscriptions alive.
        endpoints = [(self.protocol, self.keep_alive_id), *[
            (endpoint.protocol, endpoint.keep_alive_id)
            for endpoint in self._subscription_endpoints if endpoint.ready
        ]]
        responses = await asyncio.gather(*(
            protocol.send_request(GrentonCluApiPingRequest(message_id))
            for protocol, message_id in endpoints if protocol is not None
        ))
        return len(responses) == len(endpoints) and all(response is not None for response in responses)

    async def register_component_states(
        self, keys: list[StateKey], *, required_keys: set[StateKey] | None = None
    ) -> Sequence[GrentonRegistrationValue] | None:
        """Subscribe to state keys, chunked to stay under the CLU's UDP buffer.

        Keys are split into chunks of MAX_KEYS_PER_REGISTER, each registered on
        its own *persistent* UDP socket with session id 0. The CLU then pushes
        clientReport notifications on change to that socket, restoring real-time
        updates; the socket also identifies which chunk a report belongs to.

        Sockets are opened once and reused. The periodic re-register only
        refreshes registration on the existing sockets instead of
        tearing them down — recreating them drops the CLU's push channel and
        collapses updates to the re-register interval.

        Returns initial values in the same order as the input ``keys``; positions
        for chunks that failed are filled with MISSING_STATE, distinct from a real CLU nil.
        """
        async with self._registration_lock:
            if self._closed:
                return None
            if not keys:
                _LOGGER.debug("[%s] No state keys to register", self.clu.id)
                return None

            # Pack ordinary keys across widget boundaries. Large weeks remain
            # isolated without leaving a small ordinary group around each one.
            ordinary = [key for key in keys if key not in self.large_value_keys]
            chunks = [
                ordinary[i : i + MAX_KEYS_PER_REGISTER]
                for i in range(0, len(ordinary), MAX_KEYS_PER_REGISTER)
            ]
            chunks.extend([key] for key in keys if key in self.large_value_keys)

            # Rebuild sockets only when the subscribed key set actually changes
            # (e.g. after a reconfigure); otherwise keep them open so push survives.
            if [endpoint.keys for endpoint in self._subscription_endpoints] != chunks:
                await self._close_subscription_endpoints()
                for chunk in chunks:
                    await self._open_chunk_socket(chunk)
                _LOGGER.info(
                    "[%s] Tracking %d values in %d persistent subscriptions (%d large values)",
                    self.clu.id,
                    len(keys),
                    len(chunks),
                    len(keys) - len(ordinary),
                )

            for index, endpoint in enumerate(self._subscription_endpoints.copy()):
                if endpoint.protocol is None:
                    await self._open_chunk_socket(endpoint.keys)
                    if self._closed:
                        return None
                    self._subscription_endpoints[index] = (
                        self._subscription_endpoints.pop()
                    )

            values_by_key: dict[StateKey, GrentonRegistrationValue] = {}
            # A readback reuses only the affected groups. It must never replace
            # the subscription key set or open temporary subscriber sockets.
            for endpoint in self._subscription_endpoints.copy():
                if self._closed:
                    break
                if required_keys is not None and required_keys.isdisjoint(
                    endpoint.keys
                ):
                    continue
                # Keep only one registration awaiting a reply on this CLU.
                values = await self._register_chunk(endpoint)
                values_by_key.update(zip(endpoint.keys, values))
            return [values_by_key.get(key, MISSING_STATE) for key in keys]

    async def _open_chunk_socket(self, chunk: list[StateKey]) -> None:
        """Open one persistent UDP socket dedicated to a chunk of keys.

        On failure a placeholder endpoint (no transport) is still recorded so the
        chunk keeps its slot in the returned value ordering.
        """
        loop = asyncio.get_event_loop()
        if self._closed:
            return
        protocol = GrentonCluApiProtocol(self)
        protocol.subscription_keys = chunk

        # Closure binds this chunk's keys to every report arriving on this socket.
        async def on_report(values: list[GrentonValue]) -> None:
            if self.on_subscription_report is not None:
                await self.on_subscription_report(chunk, values)

        protocol.subscription_callback = on_report

        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=("0.0.0.0", 0),
            )
        except Exception as e:
            _LOGGER.error(
                "[%s] Failed to create subscription socket: %s", self.clu.id, e
            )
            self._subscription_endpoints.append(
                _SubscriptionEndpoint(None, None, chunk, 0)
            )
            return

        if self._closed:
            transport.close()
            return
        endpoint = _SubscriptionEndpoint(transport, protocol, chunk, 0, ready=False)
        # Track ownership before awaiting I/O so shutdown can close this socket.
        self._subscription_endpoints.append(endpoint)
        # Establish this endpoint's client session before registering its keys.
        # Add it to the ping loop only after this initial request has finished.
        try:
            if (
                await protocol.send_request(
                    GrentonCluApiPingRequest(endpoint.keep_alive_id)
                )
                is None
            ):
                _LOGGER.warning(
                    "[%s] Initial subscription keepalive failed", self.clu.id
                )
        except BaseException:
            transport.close()
            if endpoint in self._subscription_endpoints:
                self._subscription_endpoints.remove(endpoint)
            raise
        endpoint.ready = True
        _LOGGER.debug(
            "[%s] Opened persistent subscription socket %s to %s:%d for %d key(s)",
            self.clu.id,
            transport.get_extra_info("sockname"),
            self.clu.ip,
            self.clu.port,
            len(chunk),
        )

    async def _register_chunk(
        self, endpoint: "_SubscriptionEndpoint"
    ) -> list[GrentonRegistrationValue]:
        """(Re)register a chunk on its persistent socket; return current values.

        session id 0 mirrors the original single-subscription form the CLU
        honours for on-change push. Reports are routed by socket, not session.
        """
        if self._closed or endpoint.protocol is None:
            return [MISSING_STATE] * len(endpoint.keys)

        # Some CLU firmware needs the same registration ID on every refresh,
        # as used by the single-socket CLI, to retain unsolicited reports.
        request = GrentonCluApiClientRegisterRequest(
            endpoint.keys, endpoint.session_id, endpoint.registration_id
        )
        try:
            wire = await endpoint.protocol.send_request(request)
        except ConnectionError:
            return [MISSING_STATE] * len(endpoint.keys)
        if wire is None:
            return [MISSING_STATE] * len(endpoint.keys)
        try:
            values = GrentonCluApiClientRegisterResponse(
                wire, endpoint.keys, self.clu
            ).values
            if len(values) != len(endpoint.keys):
                raise ValueError(
                    f"Expected {len(endpoint.keys)} values, received {len(values)}"
                )
            return list(values)
        except ValueError as e:
            _LOGGER.error("[%s] Failed to parse register response: %s", self.clu.id, e)
            return [MISSING_STATE] * len(endpoint.keys)

    async def execute_action(self, action: GrentonAction) -> bool:
        """Execute an action on the CLU via the main socket."""
        if self._closed or not self.protocol:
            _LOGGER.warning("[%s] No protocol available for action execution", self.clu.id)
            return False

        request = GrentonCluApiActionRequest.from_action(action)
        async with self._action_lock:
            wire_message = await self.protocol.send_request(request)

        return wire_message is not None


class GrentonCluApiProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for one socket — main or subscription.

    Each instance owns its transport (set in connection_made) and routes
    incoming clientReport notifications to its ``subscription_callback``.
    Subscription sockets have a callback that knows their chunk's keys via
    closure; the main socket leaves the callback unset.
    """

    def __init__(self, api: GrentonCluApi):
        self.api = api
        self.transport: Optional[asyncio.DatagramTransport] = None
        self._pending: Dict[str, asyncio.Future[str]] = {}
        self._pending_lock = asyncio.Lock()
        self._response_timeout = 5.0
        self.subscription_keys: list[StateKey] | None = None
        self.subscription_callback: Optional[
            Callable[[list[GrentonValue]], Awaitable[None]]
        ] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)
        _LOGGER.debug("[%s] UDP connection established", self.api.clu.name)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._handle_incoming_data(data)

    def _handle_incoming_data(self, data: bytes) -> None:
        """Handle incoming UDP data from the CLU."""
        decrypted = self.api.cipher.decrypt(data)
        if decrypted is None:
            _LOGGER.warning("[%s] Failed to decrypt message: %s",
                            self.api.clu.name, data.hex())
            return

        try:
            plaintext = decrypted.decode().strip()
        except Exception:
            _LOGGER.warning("[%s] Failed to decode decrypted message: %s",
                            self.api.clu.name, decrypted.hex())
            return

        if not GrentonCluApiMessageParser.is_valid_response(plaintext):
            _LOGGER.debug("[%s] Received unsupported message: %s",
                          self.api.clu.name, plaintext)
            return

        self._process_response(plaintext)

    def _process_response(self, wire_message: str) -> None:
        """Process a response message from the CLU."""
        parts = wire_message.split(":", 3)
        if len(parts) != 4:
            _LOGGER.warning("[%s] Invalid wire message format", self.api.clu.name)
            return

        message_id = parts[2].lower()
        _LOGGER.debug("[%s][%s] Received: %s", self.api.clu.name, message_id, wire_message)

        async def _complete() -> None:
            async with self._pending_lock:
                fut = self._pending.pop(message_id, None)

            if fut and not fut.done():
                fut.set_result(wire_message)
            else:
                if GrentonCluApiMessageParser.is_client_report(wire_message):
                    if self.subscription_callback:
                        try:
                            notification = GrentonCluApiClientReportNotification(
                                wire_message, self.subscription_keys, self.api.clu
                            )
                        except ValueError as e:
                            _LOGGER.error("[%s] Failed to parse client report: %s",
                                          self.api.clu.name, e)
                            return
                        if self.subscription_keys is not None and len(notification.values) != len(self.subscription_keys):
                            _LOGGER.warning("[%s] Ignoring incomplete subscription report: expected %d values, received %d",
                                            self.api.clu.name, len(self.subscription_keys), len(notification.values))
                            return
                        await self.subscription_callback(notification.values)
                    else:
                        _LOGGER.debug("[%s][%s] Received report but no callback registered",
                                      self.api.clu.name, message_id)
                else:
                    _LOGGER.debug("[%s][%s] Response received with no pending request",
                                  self.api.clu.name, message_id)

        asyncio.create_task(_complete())

    async def send_request(self, request: Any, message_id: Optional[str] = None) -> Optional[str]:
        """Send a request via this socket's transport and wait for a matching response."""
        if not self.transport:
            _LOGGER.error("[%s] Transport not ready", self.api.clu.name)
            return None

        msg_id = request.msg_id
        payload = request.raw.encode()
        encrypted = self.api.cipher.encrypt(payload)

        if encrypted is None:
            _LOGGER.error("[%s] Failed to encrypt request", self.api.clu.name)
            return None

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async with self._pending_lock:
            self._pending[msg_id] = future

        try:
            async with self.api._send_lock:
                self.transport.sendto(encrypted, (self.api.clu.ip, self.api.clu.port))
                await asyncio.sleep(0.02)
            _LOGGER.debug("[%s][%s] Sent: %s", self.api.clu.name, msg_id, request.raw)
        except asyncio.CancelledError:
            async with self._pending_lock:
                self._pending.pop(msg_id, None)
            future.cancel()
            raise
        except Exception as e:
            async with self._pending_lock:
                self._pending.pop(msg_id, None)
            _LOGGER.error("[%s][%s] Failed to send request: %s",
                          self.api.clu.name, msg_id, e)
            return None

        try:
            return await asyncio.wait_for(future, timeout=self._response_timeout)
        except asyncio.TimeoutError:
            async with self._pending_lock:
                self._pending.pop(msg_id, None)
            if isinstance(request, GrentonCluApiClientRegisterRequest):
                operation = f"state read ({len(request.keys)} keys)"
            elif isinstance(request, GrentonCluApiPingRequest):
                operation = "ping"
            else:
                operation = "action write"
            _LOGGER.warning("[%s][%s] Request timeout during %s", self.api.clu.name, msg_id, operation)
            return None
        except BaseException:
            async with self._pending_lock:
                self._pending.pop(msg_id, None)
            future.cancel()
            raise

    def error_received(self, exc: Exception) -> None:
        _LOGGER.error("[%s] UDP error: %s", self.api.clu.name, exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self.transport = None
        if exc:
            _LOGGER.error("[%s] UDP connection lost: %s", self.api.clu.name, exc)
        else:
            _LOGGER.debug("[%s] UDP connection closed", self.api.clu.name)

        async def _fail_pending() -> None:
            async with self._pending_lock:
                futures = list(self._pending.values())
                self._pending.clear()

            for fut in futures:
                if not fut.done():
                    fut.set_exception(ConnectionError(
                        f"UDP connection closed for CLU {self.api.clu.name}"))

        asyncio.create_task(_fail_pending())
