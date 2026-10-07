from dataclasses import dataclass

from ..dto.clu import GrentonCluDto
from .device_types import clu_device_type


@dataclass
class GrentonClu:
    id: str
    serial_number: str
    name: str
    ip: str
    port: int

    @property
    def device_type(self) -> str | None:
        """Resolve the CLU device type from its serial number."""
        return clu_device_type(self.serial_number)

    @property
    def object_name(self) -> str:
        """The Lua object for the CLU itself, e.g. CLU521000922."""
        return f"CLU{self.serial_number}"

    @staticmethod
    def from_dto(dto: GrentonCluDto) -> "GrentonClu":
        return GrentonClu(
            id=dto.id,
            serial_number=dto.serialNumber,
            name=dto.name,
            ip=dto.ip,
            port=dto.port,
        )
