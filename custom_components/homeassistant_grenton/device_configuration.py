"""Open the existing options flow for one Grenton widget device."""

from typing import Any
from urllib.parse import quote

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .domain.entities.configurable import ConfigurableEntity

try:
    from probatio import to_field_list
except ImportError:  # Home Assistant 2025.12 uses the earlier serializer.
    from voluptuous_serialize import convert as to_field_list

DEVICE_CONFIGURATION_PANEL = "grenton-configure"
DEVICE_CONTEXT_KEY = "grenton_widget_id"


def configuration_url(entry_id: str, widget_id: str) -> str:
    return f"homeassistant://{DEVICE_CONFIGURATION_PANEL}/{quote(entry_id, safe='')}/{quote(widget_id, safe='')}"


@callback
def async_setup_device_configuration(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, async_open_device_configuration)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "grenton/device_configuration",
        vol.Required("entry_id"): str,
        vol.Required("widget_id"): str,
    }
)
@websocket_api.async_response
async def async_open_device_configuration(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if (
        entry is None
        or entry.domain != DOMAIN
        or not (runtime := getattr(entry, "runtime_data", None))
    ):
        connection.send_error(
            msg["id"], "not_found", "Grenton integration is not loaded"
        )
        return
    widget = next(
        (device for device in runtime.devices if device.id == msg["widget_id"]),
        None,
    )
    controller = (
        next(
            (
                entity
                for entity in getattr(runtime, "clu_entities", [])
                if entity.unique_id == msg["widget_id"]
            ),
            None,
        )
        if widget is None
        else None
    )
    if widget is None and controller is None:
        connection.send_error(msg["id"], "not_found", "Grenton widget was not found")
        return
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    device = next(
        (
            device
            for device in devices
            if (DOMAIN, msg["widget_id"]) in device.identifiers
        ),
        None,
    )
    entities = [
        {
            "entity_id": entity.entity_id,
            "name": entity.name,
            "configurable": isinstance(entity, ConfigurableEntity),
        }
        for entity in (widget.entities if widget else [controller])
        if entity.entity_id
    ]
    result: dict[str, Any] = {
        "device_id": device.id if device else None,
        "name": (device.name_by_user or device.name)
        if device
        else (widget.type if widget else controller.clu.name),
        "widget_type": widget.type if widget else "CLU",
        "entities": entities,
        "flow": None,
    }
    if any(entity["configurable"] for entity in entities):
        flow = await hass.config_entries.options.async_init(
            entry.entry_id, context={DEVICE_CONTEXT_KEY: widget.id}
        )
        result["flow"] = {
            **flow,
            "data_schema": to_field_list(
                flow["data_schema"], custom_serializer=cv.custom_serializer
            )
            if flow.get("data_schema") is not None
            else [],
        }
    connection.send_result(msg["id"], result)
