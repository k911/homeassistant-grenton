"""The CLI uses the shared API and releases it on cancellation or read failure."""

import asyncio
import contextlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import commands
from grenton_api import GrentonManager
from homeassistant_grenton.domain.api.clu_messages.client_register import (
    GrentonCluApiClientRegisterRequest,
)
from homeassistant_grenton.state import MISSING_STATE


class WatchAttributeCommandTests(unittest.TestCase):
    def test_zero_and_one_attribute_arguments_reach_the_watcher_once(self):
        args = commands.create_parser().parse_args([
            "watch-attribute", "--clu", "CLU_Z_WAVE_1", "--config", "unused.yaml",
            "--attribute", "CLU828599.0", "--attribute", "CLU828599.1",
            "--attribute", "CLU828599.0", "--output", "json",
        ])
        manager = SimpleNamespace(watch_attributes=AsyncMock())
        with patch("commands.watch_attribute.GrentonManager", return_value=manager):
            args.handler(args)
        manager.watch_attributes.assert_awaited_once_with(
            Path("unused.yaml"), "CLU_Z_WAVE_1", [("CLU828599", "0"), ("CLU828599", "1")],
            "json", 45,
        )


class WatchTests(unittest.IsolatedAsyncioTestCase):
    def api_and_config(self):
        api = SimpleNamespace(
            connect=AsyncMock(return_value=True),
            ping=AsyncMock(return_value=True),
            register_component_states=AsyncMock(return_value=[1, MISSING_STATE]),
            poll_component_states=AsyncMock(return_value=[42]),
            disconnect=AsyncMock(),
        )
        config = SimpleNamespace(
            clus=[SimpleNamespace(id="clu1", name="Main")],
            encryption=object(),
            cache_path=Path("unused"),
            tracked_objects_by_clu={},
        )
        return api, config

    async def test_clu_uptime_and_internal_log_use_attribute_subscription_and_full_values(self):
        api, config = self.api_and_config()
        internal_log = "first line\n" + "long diagnostic message " * 20
        api.register_component_states.return_value = [0, internal_log]
        output = io.StringIO()
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            patch("grenton_api.asyncio.sleep", side_effect=asyncio.CancelledError),
            contextlib.redirect_stdout(output),
            self.assertRaises(asyncio.CancelledError),
        ):
            await GrentonManager().watch_attributes(
                Path("unused"), "Main", [("CLU828599", "0"), ("CLU828599", "1")], "json"
            )
        keys = api.register_component_states.await_args.args[0]
        self.assertEqual(
            GrentonCluApiClientRegisterRequest(keys, 0, "1234abcd").payload,
            "SYSTEM:clientRegister(0,0,1,{{CLU828599,0},{CLU828599,1}})",
        )
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([(r["attribute"], r["value"]) for r in records], [
            ("CLU828599.0", 0), ("CLU828599.1", internal_log),
        ])
        api.disconnect.assert_awaited_once()

    async def test_watch_shares_api_skips_failed_values_and_disconnects_on_cancel(self):
        api, config = self.api_and_config()
        output = io.StringIO()
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api) as constructor,
            patch("grenton_api.asyncio.sleep", side_effect=asyncio.CancelledError),
            contextlib.redirect_stdout(output),
            self.assertRaises(asyncio.CancelledError),
        ):
            await GrentonManager().watch_variables(
                Path("unused"), "Main", ["First", "Second"], "json"
            )
        constructor.assert_called_once_with(config.clus[0], config.encryption)
        api.register_component_states.assert_awaited_once()
        self.assertEqual(
            [key.name for key in api.register_component_states.await_args.args[0]],
            ["First", "Second"],
        )
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [(record["variable"], record["value"]) for record in records],
            [("First", 1)],
        )
        api.disconnect.assert_awaited_once()

    async def test_one_off_state_read_polls_without_registering(self):
        api, config = self.api_and_config()
        manager = GrentonManager()
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            patch.object(
                manager,
                "_parse_interface_cache_for_clu",
                return_value=({"Counter": ["Counter"]}, {}),
            ),
        ):
            result = await manager.view_clu_state(Path("unused"), "Main")
        api.register_component_states.assert_not_awaited()
        api.poll_component_states.assert_awaited_once()
        self.assertEqual(result["variables"]["Counter"][0]["value"], 42)
        api.disconnect.assert_awaited_once()

    async def test_one_off_state_read_failure_still_disconnects(self):
        api, config = self.api_and_config()
        manager = GrentonManager()
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            patch.object(
                manager,
                "_parse_interface_cache_for_clu",
                side_effect=OSError("bad cache"),
            ),
            self.assertRaises(SystemExit),
        ):
            await manager.view_clu_state(Path("unused"), "Main")
        api.disconnect.assert_awaited_once()
