"""CLI-specific CLU API implementation.

This keeps the CLI's single-socket subscription behavior and count-aware
clientReport parsing separate from the Home Assistant integration API.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

from ...state import (
    GrentonCluStateAttributeKey,
    GrentonCluStateVariableKey,
    GrentonValue,
    cast_string_to_grenton_value,
)
from ..clu import GrentonClu
from ..encryption import GrentonEncryption
from .clu import GrentonCluApi, GrentonCluApiProtocol
from .clu_messages import (
    GrentonCluApiClientRegisterRequest,
    GrentonCluApiMessageParser,
)

_LOGGER = logging.getLogger(__name__)

StateKey = GrentonCluStateVariableKey | GrentonCluStateAttributeKey


def _is_escaped(content: str, index: int) -> bool:
    """Return whether the character at index follows an odd number of slashes."""
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and content[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _quote_candidates(content: str, start: int) -> list[int]:
    """Find quotes that could terminate a value starting at start."""
    candidates: list[int] = []
    for index in range(start + 1, len(content)):
        if content[index] != '"' or _is_escaped(content, index):
            continue
        remainder = content[index + 1:].lstrip()
        if not remainder or remainder.startswith(","):
            candidates.append(index)
    return candidates


def _scan_unquoted_end(content: str, start: int) -> int:
    """Find an unquoted value's end while honoring nested brackets."""
    depth = 0
    cursor = start
    while cursor < len(content):
        character = content[cursor]
        if character in "{[(":
            depth += 1
        elif character in "}])":
            depth -= 1
        elif character == "," and depth == 0:
            break
        cursor += 1
    return cursor


def _split_values(content: str, start: int, remaining: int) -> list[str] | None:
    """Split content into exactly remaining values, backtracking at quotes."""
    while start < len(content) and content[start].isspace():
        start += 1

    if remaining == 1:
        return [content[start:]]
    if start >= len(content):
        return None

    if content[start] == '"':
        for end in reversed(_quote_candidates(content, start)):
            separator = end + 1
            while separator < len(content) and content[separator].isspace():
                separator += 1
            if separator >= len(content) or content[separator] != ",":
                continue
            tail = _split_values(content, separator + 1, remaining - 1)
            if tail is not None:
                return [content[start:end + 1], *tail]
        return None

    end = _scan_unquoted_end(content, start)
    if end >= len(content):
        return None
    tail = _split_values(content, end + 1, remaining - 1)
    if tail is None:
        return None
    return [content[start:end], *tail]


def _parse_client_report(wire_message: str, expected_count: int) -> list[GrentonValue]:
    """Parse a raw clientReport without splitting commas inside values."""
    wire_parts = wire_message.split(":", 3)
    if len(wire_parts) != 4:
        raise ValueError("Invalid wire message format")

    report_parts = wire_parts[3].split(":", 2)
    if len(report_parts) != 3 or report_parts[0] != "clientReport":
        raise ValueError("Invalid clientReport payload")

    data = report_parts[2].strip()
    if not (data.startswith("{") and data.endswith("}")):
        raise ValueError("Invalid clientReport values")

    content = data[1:-1].strip()
    if not content:
        return []

    tokens = _split_values(content, 0, expected_count)
    if tokens is None:
        raise ValueError(f"Could not split clientReport into {expected_count} values")

    values: list[GrentonValue] = []
    for token in tokens:
        token = token.strip()
        if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
            token = token[1:-1]
        values.append(cast_string_to_grenton_value(token))
    return values


class GrentonCluCliApiProtocol(GrentonCluApiProtocol):
    """Protocol that parses CLI subscription reports using the key count."""

    api: "GrentonCluCliApi"

    def _process_response(self, wire_message: str) -> None:
        parts = wire_message.split(":", 3)
        if len(parts) != 4:
            _LOGGER.warning("[%s] Invalid wire message format", self.api.clu.name)
            return

        message_id = parts[2].lower()
        _LOGGER.debug("[%s][%s] Received: %s", self.api.clu.name, message_id, wire_message)

        async def complete() -> None:
            async with self._pending_lock:
                future = self._pending.pop(message_id, None)

            if future and not future.done():
                future.set_result(wire_message)
                return

            if not GrentonCluApiMessageParser.is_client_report(wire_message):
                _LOGGER.debug(
                    "[%s][%s] Response received with no pending request",
                    self.api.clu.name,
                    message_id,
                )
                return

            try:
                values = _parse_client_report(
                    wire_message,
                    len(self.api.subscription_keys),
                )
            except ValueError as error:
                _LOGGER.error("[%s] Failed to parse client report: %s", self.api.clu.name, error)
                return

            if self.api.on_subscription_report is not None:
                await self.api.on_subscription_report(self.api.subscription_keys, values)
            else:
                _LOGGER.debug(
                    "[%s][%s] Received report but no callback registered",
                    self.api.clu.name,
                    message_id,
                )

        asyncio.create_task(complete())


class GrentonCluCliApi(GrentonCluApi):
    """CLU API variant used by the CLI watch commands."""

    def __init__(self, clu: GrentonClu, encryption: GrentonEncryption):
        super().__init__(clu, encryption)
        self.subscription_keys: list[StateKey] = []
        self._subscription_message_id = secrets.token_hex(4)

    async def connect(self) -> bool:
        """Open the single socket used for pings and subscriptions."""
        try:
            loop = asyncio.get_event_loop()
            protocol = GrentonCluCliApiProtocol(self)
            self.protocol = protocol
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=("0.0.0.0", 0),
            )
            _LOGGER.debug(
                "[%s] Opened CLI UDP socket to %s:%d",
                self.clu.id,
                self.clu.ip,
                self.clu.port,
            )
            return True
        except Exception as error:
            _LOGGER.error(
                "[%s] Failed to create CLI UDP socket to %s:%d: %s",
                self.clu.id,
                self.clu.ip,
                self.clu.port,
                error,
            )
            return False

    async def register_component_states(
        self,
        keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey],
    ) -> list[GrentonValue] | None:
        """Register all watched states through the main CLI socket."""
        if not keys:
            _LOGGER.debug("[%s] No state keys to register", self.clu.id)
            return None
        if not self.protocol:
            _LOGGER.warning("[%s] No protocol available for registration", self.clu.id)
            return None

        self.subscription_keys = list(keys)
        request = GrentonCluApiClientRegisterRequest(
            keys,
            0,
            self._subscription_message_id,
        )
        wire_message = await self.protocol.send_request(request)
        if wire_message is None:
            return None

        try:
            return _parse_client_report(wire_message, len(keys))
        except ValueError as error:
            _LOGGER.error("[%s] Failed to parse register response: %s", self.clu.id, error)
            return None


__all__ = ["GrentonCluCliApi"]
