from __future__ import annotations

from ....state import (
    GrentonCluStateAttributeKey,
    GrentonCluStateVariableKey,
    GrentonValue,
    cast_string_to_grenton_value,
)
from ...scene_arguments import quote_lua_string
from .base import GrentonCluApiNotification, GrentonCluApiRequest, GrentonCluApiResponse


def _parse_client_report(
    payload: str,
    keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey] | None = None,
) -> tuple[int | None, list[GrentonValue]]:
    """Parse a clientReport payload into (session_id, values).

    Wire payload format: clientReport:{SESSION_ID}:{val1,val2,...}
    Returns (None, []) when the payload does not parse.
    """
    if not payload.startswith("clientReport:"):
        return None, []

    parts = payload.split(":", 2)
    if len(parts) != 3:
        return None, []

    try:
        session_id = int(parts[1])
    except ValueError:
        return None, []

    data_payload = parts[2]
    if not (data_payload.startswith("{") and data_payload.endswith("}")):
        return session_id, []

    content = data_payload[1:-1].strip()
    if not content:
        return session_id, []

    raw_values = content.split(",")
    values: list[GrentonValue] = []
    for position, raw_value in enumerate(raw_values):
        value = raw_value.strip().strip('"')
        key = keys[position] if keys is not None and position < len(keys) else None
        if isinstance(key, GrentonCluStateVariableKey) and key.name == "FirmwareVersion":
            # Versions are identifiers, even when they look like numbers (1.20).
            values.append(None if value == "nil" else value)
        else:
            values.append(cast_string_to_grenton_value(value))
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
            elif isinstance(key, GrentonCluStateAttributeKey): # pyright: ignore[reportUnnecessaryIsInstance]
                mapped_keys.append(f'{{{key.object_name},{key.name}}}')

        payload = f'SYSTEM:clientRegister(0,{session_id},1,{{{",".join(mapped_keys)}}})'
        super().__init__(payload, msg_id)


class GrentonCluApiClientRegisterResponse(GrentonCluApiResponse):
    """Response from client registration with initial values."""

    session_id: int | None
    values: list[GrentonValue]

    def __init__(
        self, wire_message: str,
        keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey] | None = None,
    ):
        """Initialize from wire format message and parse values."""
        super().__init__(wire_message)
        self.session_id, self.values = _parse_client_report(self.payload, keys)


class GrentonCluApiClientReportNotification(GrentonCluApiNotification):
    """Notification with subscription report and updated values."""

    session_id: int | None
    values: list[GrentonValue]

    def __init__(
        self, wire_message: str,
        keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey] | None = None,
    ):
        """Initialize from wire format message and parse values."""
        super().__init__(wire_message)
        self.session_id, self.values = _parse_client_report(self.payload, keys)
