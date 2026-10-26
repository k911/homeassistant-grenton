from __future__ import annotations

import asyncio
import logging
import secrets
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import cast

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
from .clu_health import CluConnectionHealth
from .clu_messages import (
    GrentonCluApiActionRequest,
    GrentonCluApiClientRegisterRequest,
    GrentonCluApiClientRegisterResponse,
    GrentonCluApiClientReportNotification,
    GrentonCluApiMessageParser,
    GrentonCluApiPingRequest,
    GrentonCluApiRequest,
)

_LOGGER = logging.getLogger(__name__)

# CLU's UDP buffer caps how many keys fit in one clientRegister payload.
# 30 keys is empirically safe for ordinary object names. It is a key-count
# limit, not a guarantee about reply size; large values stay isolated.
MAX_KEYS_PER_REGISTER = 30
MIN_SEND_INTERVAL = 0.02

StateKey = GrentonCluStateVariableKey | GrentonCluStateAttributeKey


@dataclass
class _SubscriptionEndpoint:
    """One UDP socket dedicated to a single sub-subscription (chunk of keys).

    Each chunk gets its own socket because CLU client_report notifications do
    not carry a usable session_id — the only way to know which chunk a report
    belongs to is to receive it on a chunk-specific socket.
    """

    transport: asyncio.DatagramTransport | None
    protocol: GrentonCluApiProtocol | None
    keys: list[StateKey]
    session_id: int
    keep_alive_id: str = field(default_factory=lambda: secrets.token_hex(4))
    registration_id: str = field(default_factory=lambda: secrets.token_hex(4))
    ready: bool = True


class GrentonCluApi:
    """Handles all communication with Grenton CLUs via UDP."""

    def __init__(
        self,
        clu: GrentonClu,
        encryption: GrentonEncryption,
        *,
        health: CluConnectionHealth | None = None,
    ):
        self.clu = clu
        self.encryption = encryption
        self.cipher = GrentonCipher(encryption)
        self.health = health if health is not None else CluConnectionHealth()

        # Serialize outgoing actions per-CLU so grouped commands don't burst
        # the shared UDP socket (CLU drops datagrams under a burst).
        self._action_lock = asyncio.Lock()
        # All sockets share one controller: space packets without serializing
        # their response waits, including during startup and keepalives.
        self._send_lock = asyncio.Lock()
        self._next_send_at = 0.0
        self._connect_lock = asyncio.Lock()
        self._generation = 0
        self._opening_protocols: set[GrentonCluApiProtocol] = set()
        # Readbacks can overlap the periodic refresh. Stable registration IDs
        # require one in-flight registration per subscription socket.
        self._registration_lock = asyncio.Lock()
        self.large_value_keys: set[StateKey] = set()
        self._closed = False

        # Main socket — shared by pings, actions and one-off polling reads.
        self.transport: asyncio.DatagramTransport | None = None
        self.protocol: GrentonCluApiProtocol | None = None
        self.keep_alive_id = secrets.token_hex(4)
        self.last_ping_summary = "no completed ping cycle"

        # One socket per chunk of subscribed keys. Reports arrive on the same
        # socket that registered them, so mapping is implicit.
        self._subscription_endpoints: list[_SubscriptionEndpoint] = []

        # Set by the coordinator; invoked with (keys, values) for every report.
        self.on_subscription_report: (
            Callable[[list[StateKey], list[GrentonValue]], Awaitable[None]] | None
        ) = None

    @property
    def subscription_count(self) -> int:
        """Count usable realtime groups, excluding the control socket."""
        return sum(
            endpoint.ready
            and endpoint.protocol is not None
            and getattr(endpoint.protocol, "closed", False) is not True
            for endpoint in self._subscription_endpoints
        )

    @property
    def log_identity(self) -> str:
        return f"{self.clu.name}/{self.clu.id}"

    def diagnostic_context(self) -> str:
        """Describe local state only; never add diagnostic CLU requests."""
        return (
            f"remote={self.clu.ip}:{self.clu.port}; "
            f"subscriptions={self.subscription_count}/{len(self._subscription_endpoints)} ready; "
            f"last_clu_response={self.health.describe_last_response()}; "
            f"total_timeouts={self.health.request_timeouts}; "
            f"read_busy={self._registration_lock.locked()}; "
            f"action_busy={self._action_lock.locked()}"
        )

    async def connect(self) -> bool:
        """Open the main UDP socket used for pings and actions."""
        generation = self._generation
        async with self._connect_lock:
            if generation != self._generation:
                return False
            if (
                self.transport is not None
                and getattr(self.protocol, "closed", False) is not True
            ):
                return True
            await self._close_socket(self.transport, self.protocol)
            if generation != self._generation:
                return False
            self.transport = None
            self._closed = False
            protocol = GrentonCluApiProtocol(self)
            self.protocol = protocol
            self._opening_protocols.add(protocol)
            try:
                (
                    transport,
                    _,
                ) = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: protocol,
                    local_addr=("0.0.0.0", 0),
                )
                # Real event loops call connection_made before returning.
                if protocol.transport is None and not protocol.closed:
                    protocol.connection_made(transport)
                if self._closed or generation != self._generation or protocol.closed:
                    await self._close_socket(transport, protocol)
                    if self.protocol is protocol:
                        self.protocol = None
                    return False
                self.transport = transport
                _LOGGER.debug(
                    "[%s] Opened main UDP socket to %s:%d",
                    self.clu.id,
                    self.clu.ip,
                    self.clu.port,
                )
                return True
            except BaseException as error:
                await self._close_socket(protocol.transport, protocol)
                if self.protocol is protocol:
                    self.protocol = None
                if isinstance(error, asyncio.CancelledError):
                    raise
                if not isinstance(error, Exception):
                    raise
                _LOGGER.error(
                    "[%s] Failed to create main UDP socket to %s:%d: %s",
                    self.clu.id,
                    self.clu.ip,
                    self.clu.port,
                    error,
                )
                return False
            finally:
                self._opening_protocols.discard(protocol)

    @staticmethod
    def _stop_socket(
        transport: asyncio.DatagramTransport | None,
        protocol: GrentonCluApiProtocol | None,
    ) -> GrentonCluApiProtocol | None:
        """Release socket ownership without yielding to cancellation or reconnects."""
        owned = None
        if isinstance(protocol, GrentonCluApiProtocol):
            owned = protocol.transport
            protocol.close()
        if (
            transport is not None
            and transport is not owned
            and getattr(transport, "is_closing", lambda: False)() is not True
        ):
            transport.close()
        return protocol if isinstance(protocol, GrentonCluApiProtocol) else None

    @staticmethod
    async def _close_socket(
        transport: asyncio.DatagramTransport | None,
        protocol: GrentonCluApiProtocol | None,
    ) -> None:
        if owned := GrentonCluApi._stop_socket(transport, protocol):
            await owned.wait_closed()

    async def _close_sockets(
        self,
        sockets: Sequence[
            tuple[asyncio.DatagramTransport | None, GrentonCluApiProtocol | None]
        ],
    ) -> None:
        """Close every transport before awaiting any consumer's cancellation."""
        protocols: set[GrentonCluApiProtocol] = set()
        for transport, protocol in sockets:
            try:
                if owned := self._stop_socket(transport, protocol):
                    protocols.add(owned)
            except Exception as error:  # noqa: BLE001 - still close the remaining sockets
                _LOGGER.error("[%s] Error closing UDP socket: %s", self.clu.id, error)
        # Await directly so wait_closed can exclude a report callback that
        # itself initiated disconnect. Child waiter tasks would deadlock it.
        for protocol in protocols:
            await protocol.wait_closed()

    def _detach_subscription_sockets(
        self,
    ) -> list[tuple[asyncio.DatagramTransport | None, GrentonCluApiProtocol | None]]:
        endpoints, self._subscription_endpoints = self._subscription_endpoints, []
        sockets = []
        for endpoint in endpoints:
            sockets.append((endpoint.transport, endpoint.protocol))
            endpoint.transport = endpoint.protocol = None
            endpoint.ready = False
        return sockets

    async def disconnect(self) -> None:
        """Close all sockets — main and per-subscription."""
        self._closed = True
        self._generation += 1
        self.on_subscription_report = None
        # Detach everything before yielding. A concurrent explicit reconnect
        # owns new sockets that this shutdown must never close.
        sockets = self._detach_subscription_sockets()
        transport, protocol = self.transport, self.protocol
        self.transport = self.protocol = None
        sockets.append((transport, protocol))
        sockets.extend(
            (opening.transport, opening) for opening in self._opening_protocols
        )
        await self._close_sockets(sockets)
        if transport:
            _LOGGER.debug("[%s] Closed main UDP socket", self.clu.id)

    async def _close_subscription_endpoints(self) -> None:
        await self._close_sockets(self._detach_subscription_sockets())

    async def ping(self) -> bool:
        """Keep the action and every subscription UDP endpoint alive."""
        if self._closed:
            self.last_ping_summary = "not attempted: API is closed"
            return False
        if (
            self.protocol is None or getattr(self.protocol, "closed", False) is True
        ) and not await self.connect():
            self.last_ping_summary = "not attempted: control socket could not be opened"
            return False

        # A CLU tracks client liveness by source UDP endpoint. Pinging only the
        # main/action socket does not keep the separate subscriptions alive.
        endpoints = [
            ("control", self.protocol, self.keep_alive_id),
            *[
                (f"subscription#{index}", endpoint.protocol, endpoint.keep_alive_id)
                for index, endpoint in enumerate(self._subscription_endpoints, start=1)
                if endpoint.ready
            ],
        ]
        targets = [
            (label, protocol, message_id)
            for label, protocol, message_id in endpoints
            if protocol is not None
        ]
        responses = await asyncio.gather(
            *(
                protocol.send_request(GrentonCluApiPingRequest(message_id))
                for _, protocol, message_id in targets
            ),
            return_exceptions=True,
        )
        failed = [
            f"{label}[{message_id}]: no protocol"
            for label, protocol, message_id in endpoints
            if protocol is None
        ]
        succeeded = 0
        for (label, protocol, message_id), response in zip(targets, responses):
            if isinstance(response, str):
                succeeded += 1
                continue
            if isinstance(response, BaseException):
                reason = f"{type(response).__name__}: {response}"
            elif isinstance(protocol, GrentonCluApiProtocol) and not protocol.active:
                reason = "socket inactive"
            else:
                reason = "no reply"
            failed.append(f"{label}[{message_id}]: {reason}")
        self.last_ping_summary = f"{succeeded}/{len(endpoints)} endpoints replied"
        if failed:
            self.last_ping_summary += "; failed=" + ", ".join(failed)
        return not failed

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
        generation = self._generation
        async with self._registration_lock:
            if self._closed or generation != self._generation:
                return None
            if not keys:
                await self._close_subscription_endpoints()
                return []

            # Reordering callers must not churn subscriptions. Only membership
            # or a newly isolated large value requires rebuilding the groups.
            subscribed = {
                key
                for endpoint in self._subscription_endpoints
                for key in endpoint.keys
            }
            rebuilt = subscribed != set(keys) or any(
                len(endpoint.keys) > 1
                and not self.large_value_keys.isdisjoint(endpoint.keys)
                for endpoint in self._subscription_endpoints
            )
            if rebuilt:
                await self._close_subscription_endpoints()
                if self._closed or generation != self._generation:
                    return None
                chunks = self._chunk_keys(keys)
                for chunk in chunks:
                    await self._open_chunk_socket(chunk)
                    if self._closed or generation != self._generation:
                        return None
                _LOGGER.info(
                    "[%s] Tracking %d values in %d persistent subscriptions (%d large values)",
                    self.clu.id,
                    len(keys),
                    len(chunks),
                    len(set(keys) & self.large_value_keys),
                )

            for endpoint in [] if rebuilt else self._subscription_endpoints.copy():
                if (
                    endpoint.protocol is None
                    or getattr(endpoint.protocol, "closed", False) is True
                ):
                    transport, protocol = endpoint.transport, endpoint.protocol
                    endpoint.transport = endpoint.protocol = None
                    await self._close_socket(transport, protocol)
                    if self._closed or generation != self._generation:
                        return None
                    await self._open_chunk_socket(endpoint.keys, replace=endpoint)
                    if self._closed or generation != self._generation:
                        return None

            values_by_key: dict[StateKey, GrentonRegistrationValue] = {}
            # A readback reuses only the affected groups. It must never replace
            # the subscription key set or open temporary subscriber sockets.
            for endpoint in self._subscription_endpoints.copy():
                if self._closed or generation != self._generation:
                    return None
                if required_keys is not None and required_keys.isdisjoint(
                    endpoint.keys
                ):
                    continue
                # Keep only one registration awaiting a reply on this CLU.
                values = await self._register_chunk(endpoint)
                if self._closed or generation != self._generation:
                    return None
                values_by_key.update(zip(endpoint.keys, values))
            return [values_by_key.get(key, MISSING_STATE) for key in keys]

    def _chunk_keys(self, keys: list[StateKey]) -> list[list[StateKey]]:
        """Batch each key once, isolating large values for both kinds of read."""
        unique_keys = list(dict.fromkeys(keys))
        ordinary = [key for key in unique_keys if key not in self.large_value_keys]
        return [
            ordinary[offset : offset + MAX_KEYS_PER_REGISTER]
            for offset in range(0, len(ordinary), MAX_KEYS_PER_REGISTER)
        ] + [[key] for key in unique_keys if key in self.large_value_keys]

    async def _open_chunk_socket(
        self, chunk: list[StateKey], *, replace: _SubscriptionEndpoint | None = None
    ) -> None:
        """Open one persistent UDP socket dedicated to a chunk of keys.

        On failure a placeholder endpoint (no transport) is still recorded so the
        chunk keeps its slot in the returned value ordering.
        """
        loop = asyncio.get_running_loop()
        generation = self._generation
        if self._closed:
            return
        protocol = GrentonCluApiProtocol(self)
        chunk = chunk.copy()
        protocol.subscription_keys = chunk
        endpoint = _SubscriptionEndpoint(None, protocol, chunk, 0, ready=False)
        if replace is None:
            self._subscription_endpoints.append(endpoint)
        else:
            self._subscription_endpoints[
                self._subscription_endpoints.index(replace)
            ] = endpoint

        # Closure binds this chunk's keys to every report arriving on this socket.
        async def on_report(values: list[GrentonValue]) -> None:
            if self.on_subscription_report is not None:
                await self.on_subscription_report(chunk, values)

        protocol.subscription_callback = on_report
        self._opening_protocols.add(protocol)
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=("0.0.0.0", 0),
            )
        except BaseException as e:
            await self._close_socket(protocol.transport, protocol)
            endpoint.protocol = None
            if not isinstance(e, Exception):
                if endpoint in self._subscription_endpoints:
                    self._subscription_endpoints.remove(endpoint)
                raise
            if self._closed or generation != self._generation:
                return
            _LOGGER.error(
                "[%s] Failed to create subscription socket: %s", self.clu.id, e
            )
            return
        finally:
            self._opening_protocols.discard(protocol)

        if protocol.transport is None and not protocol.closed:
            protocol.connection_made(transport)
        if self._closed or generation != self._generation or protocol.closed:
            await self._close_socket(transport, protocol)
            return
        endpoint.transport = transport
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
        except BaseException as error:
            failed_transport, failed_protocol = endpoint.transport, endpoint.protocol
            endpoint.transport = endpoint.protocol = None
            await self._close_socket(failed_transport, failed_protocol)
            if isinstance(error, ConnectionError):
                # Keep this group's placeholder so the next refresh replaces
                # only its socket, without disturbing healthy subscriptions.
                return
            if endpoint in self._subscription_endpoints:
                self._subscription_endpoints.remove(endpoint)
            raise
        if self._closed or generation != self._generation:
            return
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
        self, endpoint: _SubscriptionEndpoint
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
        return self._parse_state_response(wire, endpoint.keys)

    def _parse_state_response(
        self,
        wire: str,
        keys: list[StateKey],
    ) -> list[GrentonRegistrationValue]:
        """Parse a reply already correlated by its socket and request message ID.

        The report's numeric field is not a reliable echo of the requested
        session. It must not be used to reject an otherwise matched reply.
        """
        try:
            response = GrentonCluApiClientRegisterResponse(wire, keys, self.clu)
            values = response.values
            if len(values) != len(keys):
                raise ValueError(f"Expected {len(keys)} values, received {len(values)}")
            return list(values)
        except ValueError as e:
            _LOGGER.error("[%s] Failed to parse register response: %s", self.clu.id, e)
            return [MISSING_STATE] * len(keys)

    async def poll_component_states(
        self, keys: list[StateKey]
    ) -> list[GrentonRegistrationValue]:
        """Read session 1 on the existing control socket without realtime groups."""
        generation = self._generation
        async with self._registration_lock:
            if self._closed or generation != self._generation:
                return [MISSING_STATE] * len(keys)
            values_by_key: dict[StateKey, GrentonRegistrationValue] = {}
            for chunk in self._chunk_keys(keys):
                wire = None
                if not self._closed and self.protocol is not None:
                    # Batch keys can differ: fresh IDs prevent delayed replies
                    # from a previous poll being assigned to the wrong keys.
                    request = GrentonCluApiClientRegisterRequest(chunk, 1)
                    try:
                        async with self._action_lock:
                            if not self._closed and generation == self._generation:
                                wire = await self.protocol.send_request(request)
                    except ConnectionError:
                        pass
                if self._closed or generation != self._generation:
                    return [MISSING_STATE] * len(keys)
                values = (
                    self._parse_state_response(wire, chunk)
                    if wire is not None
                    else [MISSING_STATE] * len(chunk)
                )
                values_by_key.update(zip(chunk, values))
            return [values_by_key.get(key, MISSING_STATE) for key in keys]

    async def execute_action(self, action: GrentonAction) -> bool:
        """Execute an action on the CLU via the main socket."""
        if self._closed or not self.protocol:
            _LOGGER.warning(
                "[%s] No protocol available for action execution", self.clu.id
            )
            return False

        request = GrentonCluApiActionRequest.from_action(action)
        generation = self._generation
        async with self._action_lock:
            if self._closed or generation != self._generation or self.protocol is None:
                return False
            wire_message = await self.protocol.send_request(request)

        return wire_message is not None


class GrentonCluApiProtocol(asyncio.DatagramProtocol):
    """One owned UDP endpoint, its reply waiters and notification tasks."""

    def __init__(self, api: GrentonCluApi):
        self.api = api
        self._generation = api._generation
        self.transport: asyncio.DatagramTransport | None = None
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._response_timeout = 5.0
        self.health = CluConnectionHealth()
        self._local_address = "unbound"
        self._sent_requests = 0
        self._received_replies = 0
        self._received_reports = 0
        self._discarded_packets: Counter[str] = Counter()
        self._last_udp_error: str | None = None
        self._last_udp_error_at: float | None = None
        self.closed = False
        self._report_tasks: set[asyncio.Task] = set()
        self.subscription_keys: list[StateKey] | None = None
        self.subscription_callback: (
            Callable[[list[GrentonValue]], Awaitable[None]] | None
        ) = None

    def diagnostic_context(self) -> str:
        """Identify this socket even if another socket still receives replies."""
        role = "control"
        key_summary = ""
        if self.subscription_keys is not None:
            role = "subscription"
            for index, endpoint in enumerate(self.api._subscription_endpoints, start=1):
                if endpoint.protocol is self:
                    role = f"subscription#{index}"
                    break
            names = [
                f"{key.object_name}.{key.name}"
                if isinstance(key, GrentonCluStateAttributeKey)
                else f"var:{key.name}"
                for key in self.subscription_keys[:3]
            ]
            if len(self.subscription_keys) > 3:
                names.append(f"+{len(self.subscription_keys) - 3} more")
            key_summary = f"; keys={len(self.subscription_keys)} [{', '.join(names)}]"
        discarded = (
            ",".join(
                f"{reason}:{count}" for reason, count in self._discarded_packets.items()
            )
            or "none"
        )
        pending = sum(not future.done() for future in self._pending.values())
        udp_error = self._last_udp_error or "none"
        if self._last_udp_error_at is not None:
            udp_error += (
                f" ({max(0.0, monotonic() - self._last_udp_error_at):.1f}s ago)"
            )
        return (
            f"endpoint={role}; local={self._local_address}; active={self.active}{key_summary}; "
            f"last_endpoint_response={self.health.describe_last_response()}; "
            f"sent={self._sent_requests}, replies={self._received_replies}, reports={self._received_reports}, "
            f"endpoint_timeouts={self.health.request_timeouts}, pending={pending}; "
            f"discarded={discarded}; last_udp_error={udp_error}; "
            f"{self.api.diagnostic_context()}"
        )

    def _record_response(self) -> None:
        self.health.record_response()
        self.api.health.record_response()

    @property
    def active(self) -> bool:
        """A detached socket cannot resume when the same API reconnects."""
        return (
            not self.closed
            and not self.api._closed
            and self._generation == self.api._generation
        )

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not self.active:
            transport.close()
            return
        self.transport = cast(asyncio.DatagramTransport, transport)
        address = transport.get_extra_info("sockname")
        if isinstance(address, tuple) and len(address) >= 2:
            self._local_address = f"{address[0]}:{address[1]}"
        _LOGGER.debug("[%s] UDP connection established", self.api.clu.name)

    def close(self) -> None:
        """Stop dispatch synchronously, including before connection_lost runs."""
        if self.closed:
            return
        self.closed = True
        self.subscription_callback = None
        transport, self.transport = self.transport, None
        futures, self._pending = list(self._pending.values()), {}
        for future in futures:
            if not future.done():
                future.set_exception(
                    ConnectionError(
                        f"UDP connection closed for CLU {self.api.clu.name}"
                    )
                )
        for task in self._report_tasks:
            if task is not asyncio.current_task():
                task.cancel()
        if transport is not None:
            transport.close()

    async def wait_closed(self) -> None:
        tasks = [
            task for task in self._report_tasks if task is not asyncio.current_task()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr != (self.api.clu.ip, self.api.clu.port):
            self._discarded_packets["wrong_peer"] += 1
            _LOGGER.debug(
                "[%s] Ignoring datagram from unexpected peer %s", self.api.clu.id, addr
            )
            return
        self._handle_incoming_data(data)

    def _handle_incoming_data(self, data: bytes) -> None:
        if not self.active:
            return
        decrypted = self.api.cipher.decrypt(data)
        if decrypted is None:
            self._discarded_packets["decryption"] += 1
            _LOGGER.warning(
                "[%s] Failed to decrypt CLU message; %s",
                self.api.log_identity,
                self.diagnostic_context(),
            )
            return
        try:
            plaintext = decrypted.decode().strip()
        except UnicodeDecodeError:
            self._discarded_packets["encoding"] += 1
            _LOGGER.warning(
                "[%s] Failed to decode CLU message; %s",
                self.api.log_identity,
                self.diagnostic_context(),
            )
            return
        self._process_response(plaintext)

    def _report_values(self, wire_message: str) -> list[GrentonValue]:
        notification = GrentonCluApiClientReportNotification(
            wire_message, self.subscription_keys, self.api.clu
        )
        if self.subscription_keys is not None and len(notification.values) != len(
            self.subscription_keys
        ):
            raise ValueError("Incomplete subscription report")
        return notification.values

    async def _deliver_report(self, values: list[GrentonValue]) -> None:
        try:
            if self.active and self.subscription_callback is not None:
                await self.subscription_callback(values)
        except Exception as error:  # noqa: BLE001 - a bad consumer must not break other reports
            _LOGGER.error(
                "[%s] Could not process subscription report: %s", self.api.clu.id, error
            )

    def _process_response(self, wire_message: str) -> None:
        if not self.active:
            return
        response = GrentonCluApiMessageParser.parse_response(wire_message)
        if response is None:
            self._discarded_packets["invalid_message"] += 1
            _LOGGER.debug("[%s] Received unsupported message", self.api.clu.name)
            return
        message_id = response.msg_id
        _LOGGER.debug(
            "[%s][%s] Received: %s", self.api.clu.name, message_id, wire_message
        )
        future = self._pending.pop(message_id, None)
        if future is not None and not future.done():
            self._received_replies += 1
            self._record_response()
            future.set_result(wire_message)
            return
        if not response.is_client_report or self.subscription_callback is None:
            self._discarded_packets["unmatched_reply"] += 1
            return
        try:
            values = self._report_values(wire_message)
        except ValueError as error:
            self._discarded_packets["invalid_report"] += 1
            _LOGGER.error(
                "[%s] Failed to parse client report: %s; %s",
                self.api.log_identity,
                error,
                self.diagnostic_context(),
            )
            return
        task = asyncio.create_task(self._deliver_report(values))
        self._received_reports += 1
        self._record_response()
        self._report_tasks.add(task)
        task.add_done_callback(self._report_tasks.discard)

    async def send_request(self, request: GrentonCluApiRequest) -> str | None:
        """Pace transmission, then await exactly this request's reply."""
        if not self.active or self.transport is None:
            return None
        msg_id = request.msg_id
        encrypted = self.api.cipher.encrypt(request.raw.encode())
        if encrypted is None:
            _LOGGER.error(
                "[%s][%s] Failed to encrypt request; %s",
                self.api.log_identity,
                msg_id,
                self.diagnostic_context(),
            )
            return None
        future: asyncio.Future[str] | None = None
        loop = asyncio.get_running_loop()
        queued_at = loop.time()
        sent_at: float | None = None
        reports_at_send = self._received_reports
        replies_at_send = self._received_replies
        other_requests_at_send = 0
        try:
            async with self.api._send_lock:
                delay = self.api._next_send_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if not self.active or self.transport is None:
                    return None
                if msg_id in self._pending:
                    _LOGGER.warning(
                        "[%s][%s] Duplicate in-flight request ID; %s",
                        self.api.log_identity,
                        msg_id,
                        self.diagnostic_context(),
                    )
                    return None
                # Do not accept stale replies while waiting for the send gate.
                reports_at_send = self._received_reports
                replies_at_send = self._received_replies
                other_requests_at_send = sum(
                    not pending.done() for pending in self._pending.values()
                )
                future = loop.create_future()
                self._pending[msg_id] = future
                self.transport.sendto(encrypted, (self.api.clu.ip, self.api.clu.port))
                self._sent_requests += 1
                sent_at = loop.time()
                self.api._next_send_at = loop.time() + MIN_SEND_INTERVAL
            _LOGGER.debug("[%s][%s] Sent: %s", self.api.clu.name, msg_id, request.raw)
            return await asyncio.wait_for(future, timeout=self._response_timeout)
        except TimeoutError:
            self.api.health.request_timeouts += 1
            self.health.request_timeouts += 1
            if isinstance(request, GrentonCluApiClientRegisterRequest):
                operation = f"{'slow' if request.session_id == 1 else 'state'} read ({len(request.keys)} keys)"
            elif isinstance(request, GrentonCluApiPingRequest):
                operation = "ping"
            else:
                operation = "action write"
            _LOGGER.warning(
                "[%s][%s] Request timeout during %s after %.2fs "
                "(limit=%.2fs, queued=%.3fs); reports_during_wait=%d, "
                "other_replies_during_wait=%d, other_requests_in_flight_at_send=%d; %s",
                self.api.log_identity,
                msg_id,
                operation,
                loop.time() - sent_at if sent_at is not None else 0,
                self._response_timeout,
                sent_at - queued_at if sent_at is not None else loop.time() - queued_at,
                self._received_reports - reports_at_send,
                self._received_replies - replies_at_send,
                other_requests_at_send,
                self.diagnostic_context(),
            )
            return None
        except (ConnectionError, asyncio.CancelledError):
            raise
        except Exception as error:  # noqa: BLE001 - contain transport errors; never replay writes
            _LOGGER.error(
                "[%s][%s] Failed to send request: %s: %s; %s",
                self.api.log_identity,
                msg_id,
                type(error).__name__,
                error,
                self.diagnostic_context(),
            )
            return None
        finally:
            if future is not None:
                if self._pending.get(msg_id) is future:
                    self._pending.pop(msg_id)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    # A close during send/pacing can fail the waiter before it
                    # is awaited. Always retrieve its exception during cleanup.
                    future.exception()

    def error_received(self, exc: Exception) -> None:
        self._last_udp_error = f"{type(exc).__name__}: {exc}"
        self._last_udp_error_at = monotonic()
        _LOGGER.error(
            "[%s] UDP error: %s; %s",
            self.api.log_identity,
            self._last_udp_error,
            self.diagnostic_context(),
        )

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None
        self.close()
        if exc:
            self._last_udp_error = f"{type(exc).__name__}: {exc}"
            self._last_udp_error_at = monotonic()
            _LOGGER.error(
                "[%s] UDP connection lost: %s; %s",
                self.api.log_identity,
                self._last_udp_error,
                self.diagnostic_context(),
            )
        else:
            _LOGGER.debug("[%s] UDP connection closed", self.api.clu.name)
