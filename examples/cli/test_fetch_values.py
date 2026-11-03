"""fetch-values reads once, keeps failures explicit, and supports safe summaries."""

import asyncio
import contextlib
import io
import json
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from commands import create_parser
from grenton_api import _CLU_WIRE_LOGGER, GrentonManager
from homeassistant_grenton.state import MISSING_STATE


class FetchValuesTests(unittest.IsolatedAsyncioTestCase):
    def api_and_config(self, values):
        api = SimpleNamespace(
            connect=AsyncMock(return_value=True),
            fetch_component_states=AsyncMock(return_value=values),
            disconnect=AsyncMock(),
        )
        config = SimpleNamespace(
            clus=[SimpleNamespace(id="clu1", name="Main")], encryption=object()
        )
        return api, config

    async def test_selected_attributes_and_variables_are_deduplicated_without_subscribing(
        self,
    ):
        api, config = self.api_and_config([21.5, None, False])
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
        ):
            result = await GrentonManager().fetch_values(
                Path("unused"),
                "Main",
                [("THERMOSTAT1", "14"), ("CLU1", "0"), ("THERMOSTAT1", "14")],
                ["Enabled", " Enabled ", ""],
            )
        keys = api.fetch_component_states.await_args.args[0]
        self.assertEqual([key.name for key in keys], ["14", "0", "Enabled"])
        self.assertEqual(
            result["values"],
            [
                {"key": "THERMOSTAT1.14", "type": "float", "value": 21.5},
                {"key": "CLU1.0", "type": "nil", "value": None},
                {"key": "Enabled", "type": "boolean", "value": False},
            ],
        )
        self.assertTrue(result["complete"])
        api.connect.assert_awaited_once()
        api.fetch_component_states.assert_awaited_once()
        api.disconnect.assert_awaited_once()

    async def test_summary_omits_values_and_suppresses_wire_debug_logging(self):
        secret = "private-schedule-contents"
        api, config = self.api_and_config([secret, 42, False, None])
        original_level = _CLU_WIRE_LOGGER.level
        messages = []

        class Capture(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = Capture()
        _CLU_WIRE_LOGGER.addHandler(handler)
        _CLU_WIRE_LOGGER.setLevel(logging.DEBUG)

        async def fetch(keys):
            _CLU_WIRE_LOGGER.debug("Raw CLU reply: %s", secret)
            return [secret, 42, False, None]

        api.fetch_component_states.side_effect = fetch
        try:
            with (
                patch("grenton_api.load_configuration", return_value=config),
                patch("grenton_api.GrentonCluApi", return_value=api),
            ):
                result = await GrentonManager().fetch_values(
                    Path("unused"),
                    "Main",
                    [("THERMOSTAT1", "9")],
                    ["Counter", "Enabled", "Empty"],
                    summary=True,
                )
            self.assertEqual(_CLU_WIRE_LOGGER.level, logging.DEBUG)
            self.assertNotIn(secret, json.dumps(result) + " ".join(messages))
            self.assertEqual(
                result["values"],
                [
                    {"key": "THERMOSTAT1.9", "type": "string", "length": len(secret)},
                    {"key": "Counter", "type": "integer"},
                    {"key": "Enabled", "type": "boolean"},
                    {"key": "Empty", "type": "nil"},
                ],
            )
        finally:
            _CLU_WIRE_LOGGER.removeHandler(handler)
            _CLU_WIRE_LOGGER.setLevel(original_level)
        api.disconnect.assert_awaited_once()

    async def test_failed_read_is_distinct_from_real_nil(self):
        api, config = self.api_and_config([None, MISSING_STATE])
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
        ):
            result = await GrentonManager().fetch_values(
                Path("unused"), "Main", [], ["Empty", "Failed"]
            )
        self.assertFalse(result["complete"])
        self.assertEqual(
            result["values"][0], {"key": "Empty", "type": "nil", "value": None}
        )
        self.assertEqual(
            result["values"][1],
            {"key": "Failed", "type": "missing", "status": "missing"},
        )
        api.disconnect.assert_awaited_once()

    async def test_empty_selection_fails_before_loading_config_or_connecting(self):
        with (
            patch("grenton_api.load_configuration") as load,
            self.assertRaises(SystemExit) as failure,
        ):
            await GrentonManager().fetch_values(Path("unused"), "Main", [], ["  "])
        self.assertEqual(failure.exception.code, 2)
        load.assert_not_called()

    async def test_connect_failure_still_disconnects_without_sending_fetch(self):
        api, config = self.api_and_config([1])
        api.connect.return_value = False
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            self.assertRaises(SystemExit) as failure,
        ):
            await GrentonManager().fetch_values(Path("unused"), "Main", [], ["Counter"])
        self.assertEqual(failure.exception.code, 1)
        api.fetch_component_states.assert_not_awaited()
        api.disconnect.assert_awaited_once()

    async def test_cancelled_fetch_disconnects_and_restores_logging(self):
        api, config = self.api_and_config([1])
        api.fetch_component_states.side_effect = asyncio.CancelledError
        original_level = _CLU_WIRE_LOGGER.level
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            self.assertRaises(asyncio.CancelledError),
        ):
            await GrentonManager().fetch_values(
                Path("unused"), "Main", [], ["Counter"], summary=True
            )
        api.disconnect.assert_awaited_once()
        self.assertEqual(_CLU_WIRE_LOGGER.level, original_level)

    async def test_incomplete_response_still_disconnects(self):
        api, config = self.api_and_config([])
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            self.assertRaises(SystemExit) as failure,
        ):
            await GrentonManager().fetch_values(Path("unused"), "Main", [], ["Counter"])
        self.assertEqual(failure.exception.code, 1)
        api.disconnect.assert_awaited_once()


class FetchValuesCommandTests(unittest.TestCase):
    def test_parser_dispatches_mixed_keys_and_json_summary(self):
        result = {
            "clu": {"name": "Main", "id": "clu1"},
            "values": [{"key": "THERMOSTAT1.9", "type": "string", "length": 1350}],
            "complete": True,
        }
        args = create_parser().parse_args(
            [
                "--debug",
                "fetch-values",
                "--clu",
                "Main",
                "--config",
                "custom.yaml",
                "--attribute",
                "THERMOSTAT1.9",
                "--variable",
                "Counter",
                "--format",
                "json",
                "--summary",
            ]
        )
        output = io.StringIO()
        with (
            patch(
                "commands.fetch_values.GrentonManager.fetch_values",
                new_callable=AsyncMock,
                return_value=result,
            ) as fetch,
            contextlib.redirect_stdout(output),
        ):
            args.handler(args)
        fetch.assert_awaited_once_with(
            Path("custom.yaml"),
            "Main",
            [("THERMOSTAT1", "9")],
            ["Counter"],
            summary=True,
        )
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_missing_result_is_printed_and_command_exits_unsuccessfully(self):
        result = {
            "clu": {"name": "Main", "id": "clu1"},
            "values": [{"key": "Missing", "status": "missing", "type": "missing"}],
            "complete": False,
        }
        args = create_parser().parse_args(["fetch-values", "--variable", "Missing"])
        output = io.StringIO()
        with (
            patch(
                "commands.fetch_values.GrentonManager.fetch_values",
                new_callable=AsyncMock,
                return_value=result,
            ),
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as failure,
        ):
            args.handler(args)
        self.assertEqual(failure.exception.code, 1)
        self.assertIn("Missing: <read failed>", output.getvalue())

    def test_invalid_attribute_fails_before_constructing_manager(self):
        args = create_parser().parse_args(["fetch-values", "--attribute", "invalid"])
        with (
            patch("commands.fetch_values.GrentonManager") as manager,
            self.assertRaises(SystemExit),
        ):
            args.handler(args)
        manager.assert_not_called()

    def test_attribute_rejects_raw_lua_before_loading_or_connecting(self):
        for spec in (
            "THE6183.10);Script()",
            "Script().9",
            "THE6183.-1",
            "THE6183.Value",
        ):
            with self.subTest(spec=spec):
                args = create_parser().parse_args(["fetch-values", "--attribute", spec])
                with (
                    patch("commands.fetch_values.GrentonManager") as manager,
                    self.assertRaises(SystemExit) as failure,
                ):
                    args.handler(args)
                self.assertEqual(failure.exception.code, 2)
                manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
