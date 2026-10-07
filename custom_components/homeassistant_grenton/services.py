"""Integration actions targeting discovered CLU controllers."""

import asyncio

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import service
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .domain.entities.clu import GrentonCluEntity
from .domain.entities.scene_configuration import scene_arguments_selector

SERVICE_RUN_SCRIPT = "run_script"
SERVICE_RUN_SCRIPT_SCHEMA = {
    vol.Required("script"): vol.All(str, str.strip, vol.Length(min=1)),
    vol.Optional("arguments", default=list): scene_arguments_selector(),
}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register once, independently of loaded entries and their platforms."""
    service.async_register_batched_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RUN_SCRIPT,
        entity_domain=Platform.SENSOR,
        schema=SERVICE_RUN_SCRIPT_SCHEMA,
        func=_async_run_script,
    )


async def _async_run_script(entities: list[Entity], call: ServiceCall) -> None:
    """Select controllers, preserving HA target resolution and permissions."""
    # Device/area targets can include other Grenton sensors. Only controllers
    # can execute scripts; selecting an ordinary sensor alone is an error.
    controllers = [entity for entity in entities if isinstance(entity, GrentonCluEntity)]
    if not controllers:
        raise ServiceValidationError("Select a Grenton CLU controller entity or device")
    await asyncio.gather(
        *(
            entity.async_run_script(call.data["script"], call.data["arguments"])
            for entity in controllers
        )
    )
