from __future__ import annotations

from ....state import (
    GrentonCluStateAttributeKey,
    GrentonCluStateVariableKey,
    GrentonValue,
    cast_string_to_grenton_value,
)
from ...clu import GrentonClu
from ...device_types import attribute_index
from ...scene_arguments import quote_lua_string
from .base import GrentonCluApiNotification, GrentonCluApiRequest, GrentonCluApiResponse
from .report_values import split_report_values


def _parse_client_report(
    payload: str,
    keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey] | None = None,
    clu: GrentonClu | None = None,
) -> tuple[int, list[GrentonValue]]:
    """Parse a clientReport payload into (session_id, values).

    Wire payload format: clientReport:{SESSION_ID}:{val1,val2,...}
    Reject malformed headers before their values can affect state or health.
    """
    if not payload.startswith("clientReport:"):
        raise ValueError("Expected a clientReport payload")

    parts = payload.split(":", 2)
    if len(parts) != 3:
        raise ValueError("Incomplete clientReport header")

    try:
        session_id = int(parts[1])
    except ValueError as error:
        raise ValueError("Invalid clientReport session") from error
    if session_id < 0:
        raise ValueError("Invalid clientReport session")

    data_payload = parts[2]
    if not (data_payload.startswith("{") and data_payload.endswith("}")):
        raise ValueError("Expected clientReport values in braces")

    content = data_payload[1:-1].strip()
    if not content:
        return session_id, []

    # Schedule strings contain commas; only split between Lua values.
    raw_values = split_report_values(content, len(keys) if keys is not None else None)
    values: list[GrentonValue] = []
    for position, raw_value in enumerate(raw_values):
        token = raw_value.strip()
        quoted = len(token) >= 2 and token.startswith('"') and token.endswith('"')
        value = token[1:-1] if quoted else token
        key = keys[position] if keys is not None and position < len(keys) else None
        if (
            isinstance(key, GrentonCluStateAttributeKey)
            and clu is not None
            and key.object_name == clu.object_name
            and key.name == attribute_index(clu.device_type, "FirmwareVersion")
        ):
            # Versions are identifiers, even when they look like numbers (1.20).
            values.append(None if value == "nil" else value)
        else:
            values.append(
                cast_string_to_grenton_value(value, preserve_string_whitespace=quoted)
            )
    return session_id, values


class GrentonCluApiClientRegisterRequest(GrentonCluApiRequest):
    """Request to register component states for subscription."""

    keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey]
    session_id: int

    def __init__(
        self,
        keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey],
        session_id: int,
        msg_id: str | None = None,
    ):
        """Initialize with state keys and session id to register."""
        self.keys = keys
        self.session_id = session_id

        if not keys:
            raise ValueError("Cannot create registration request with no keys")

        mapped_keys: list[str] = []

        for key in keys:
            if isinstance(key, GrentonCluStateVariableKey):
                mapped_keys.append(quote_lua_string(key.name))
            elif isinstance(key, GrentonCluStateAttributeKey):  # pyright: ignore[reportUnnecessaryIsInstance]
                mapped_keys.append(f"{{{key.object_name},{key.name}}}")

        payload = f"SYSTEM:clientRegister(0,{session_id},1,{{{','.join(mapped_keys)}}})"
        super().__init__(payload, msg_id)


class GrentonCluApiClientRegisterResponse(GrentonCluApiResponse):
    """Response from client registration with initial values."""

    session_id: int
    values: list[GrentonValue]

    def __init__(
        self,
        wire_message: str,
        keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey]
        | None = None,
        clu: GrentonClu | None = None,
    ):
        """Initialize from wire format message and parse values."""
        super().__init__(wire_message)
        self.session_id, self.values = _parse_client_report(self.payload, keys, clu)


class GrentonCluApiClientReportNotification(GrentonCluApiNotification):
    """Notification with subscription report and updated values."""

    session_id: int
    values: list[GrentonValue]

    def __init__(
        self,
        wire_message: str,
        keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey]
        | None = None,
        clu: GrentonClu | None = None,
    ):
        """Initialize from wire format message and parse values."""
        super().__init__(wire_message)
        self.session_id, self.values = _parse_client_report(self.payload, keys, clu)
