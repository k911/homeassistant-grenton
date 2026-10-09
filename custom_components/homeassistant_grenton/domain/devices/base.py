from abc import ABC
from dataclasses import dataclass

from homeassistant.helpers.device_registry import DeviceInfo

from ..entities.base import BaseGrentonEntity


@dataclass
class BaseGrentonDevice(ABC):
    type: str
    id: str
    entities: list[BaseGrentonEntity]

    @property
    def name(self) -> str:
        """Summarize the widget using its distinct entity labels."""
        labels = list(
            dict.fromkeys(
                entity.label.strip() for entity in self.entities if entity.label.strip()
            )
        )
        if not labels:
            return f"{self.type} ({self.id})"
        name = " · ".join(labels[:2])
        if len(labels) > 2:
            name += f" (+{len(labels) - 2})"
        return name

    @property
    def device_info(self) -> DeviceInfo:
        """Return Home Assistant device info."""
        return DeviceInfo(
            identifiers={("grenton", self.id)},
            manufacturer="Grenton",
            model=self.type,
            name=self.name,
        )
