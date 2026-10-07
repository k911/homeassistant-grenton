"""Configuration types for Grenton integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .coordinator import GrentonCoordinator
    from .domain.devices.base import BaseGrentonDevice
    from .domain.entities.clu import GrentonCluEntity
    from .domain.entities.clu_state import GrentonCluStateEntity


class GrentonConfigEntryData(TypedDict):
    """Type definition for config entry data."""

    ip_address: str
    port: int
    pin: str
    interface: dict[str, Any]


@dataclass
class RuntimeData:
    """Runtime data stored in config entry."""

    coordinator: GrentonCoordinator
    devices: list[BaseGrentonDevice]
    clu_entities: list[GrentonCluEntity | GrentonCluStateEntity] = field(
        default_factory=list
    )


type GrentonConfigEntry = ConfigEntry[RuntimeData]
