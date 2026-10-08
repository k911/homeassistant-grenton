"""Check that the editor's JavaScript is actually registered for the UI."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant import components

from custom_components.homeassistant_grenton.frontend import async_register_scene_editor


def test_scene_editor_assets_register_once_for_multiple_config_entries():
    frontend = SimpleNamespace(
        add_extra_js_url=Mock(), async_register_built_in_panel=Mock()
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(components={"frontend"}),
        data={},
        http=SimpleNamespace(async_register_static_paths=AsyncMock()),
        async_add_executor_job=AsyncMock(side_effect=lambda job: job()),
    )

    async def run():
        with patch.object(components, "frontend", frontend, create=True):
            await asyncio.gather(
                async_register_scene_editor(hass),
                async_register_scene_editor(hass),
            )

    asyncio.run(run())
    hass.http.async_register_static_paths.assert_awaited_once()
    (static_path,) = hass.http.async_register_static_paths.await_args.args[0]
    assert static_path.url_path.startswith("/grenton/scene-editor-")
    assert static_path.url_path.endswith(".js")
    assert Path(static_path.path).is_file()
    assert static_path.cache_headers is True
    frontend.add_extra_js_url.assert_called_once_with(hass, static_path.url_path)
    assert hass.data["grenton"]["scene_editor_url"] == static_path.url_path
    frontend.async_register_built_in_panel.assert_not_called()


def test_headless_home_assistant_does_not_load_frontend_assets():
    hass = SimpleNamespace(config=SimpleNamespace(components=set()))
    asyncio.run(async_register_scene_editor(hass))
