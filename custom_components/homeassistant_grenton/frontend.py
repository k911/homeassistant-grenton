"""Load the scene selectors in Home Assistant's frontend."""

import asyncio
import hashlib
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .device_configuration import DEVICE_CONFIGURATION_PANEL


async def async_register_scene_editor(hass: HomeAssistant) -> None:
    """Register the module once, with a content-based URL to avoid stale UI."""
    if "frontend" not in hass.config.components:
        return
    from homeassistant.components import frontend
    from homeassistant.components.http import StaticPathConfig

    data = hass.data.setdefault(DOMAIN, {})
    lock = data.setdefault("scene_editor_lock", asyncio.Lock())
    async with lock:
        if "scene_editor_url" in data:
            return
        path = Path(__file__).parent / "frontend" / "scene-editor.js"
        digest = await hass.async_add_executor_job(
            lambda: hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        )
        url = f"/grenton/scene-editor-{digest}.js"
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(url, str(path), True),
            ]
        )
        frontend.add_extra_js_url(hass, url)
        from homeassistant.components.panel_custom import async_register_panel

        await async_register_panel(
            hass,
            frontend_url_path=DEVICE_CONFIGURATION_PANEL,
            webcomponent_name="grenton-device-configuration",
            module_url=url,
            require_admin=True,
        )
        data["scene_editor_url"] = url
