"""CLI action selection and exact CLU payloads, with no live connection."""

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from commands import create_parser
from homeassistant_grenton.domain.api.clu_messages.action import (
    GrentonCluApiActionRequest,
)


class ExecuteTests(unittest.TestCase):
    def run_command(self, extra=(), *, result=True, error=None, connected=True, base_args=None):
        if base_args is None:
            raw_args = [
                "execute",
                "--clu",
                "Main",
                "--object",
                "THERMOSTAT1",
                "--action",
                "10",
                "--value",
                "10",
                *extra,
            ]
        else:
            raw_args = ["execute", *base_args, *extra]
        args = create_parser().parse_args(raw_args)
        config = SimpleNamespace(
            clus=[SimpleNamespace(id="clu1", name="Main")],
            encryption=object(),
        )
        api = SimpleNamespace(
            connect=AsyncMock(return_value=connected),
            execute_action=AsyncMock(return_value=result, side_effect=error),
            disconnect=AsyncMock(),
        )
        output = io.StringIO()
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as exit_result,
        ):
            args.handler(args)
        if exit_result.exception.code != 2:
            api.disconnect.assert_awaited_once()
        return api, exit_result.exception.code, output.getvalue()

    def test_existing_execute_defaults_to_method(self):
        api, status, output = self.run_command()
        action = api.execute_action.await_args.args[0]
        self.assertEqual(
            GrentonCluApiActionRequest.from_action(action).payload,
            'THERMOSTAT1:execute(10,"10")',
        )
        self.assertEqual(status, 0)
        self.assertIn('.execute(10, "10")', output)

    def test_attribute_option_writes_instead_of_calling_a_method(self):
        api, status, output = self.run_command(("--call-type", "attribute"))
        action = api.execute_action.await_args.args[0]
        self.assertEqual(
            GrentonCluApiActionRequest.from_action(action).payload,
            'THERMOSTAT1:set(10,"10")',
        )
        self.assertEqual(action.clu_id, "clu1")
        self.assertEqual(status, 0)
        self.assertIn('.set(10, "10")', output)

    def test_explicit_method_is_supported(self):
        api, status, _ = self.run_command(("--call-type", "method"))
        action = api.execute_action.await_args.args[0]
        self.assertEqual(
            GrentonCluApiActionRequest.from_action(action).payload,
            'THERMOSTAT1:execute(10,"10")',
        )
        self.assertEqual(status, 0)

    def test_script_execution_without_arguments(self):
        api, status, output = self.run_command(
            base_args=["--clu", "Main", "--object", "Evening", "--call-type", "script"]
        )
        action = api.execute_action.await_args.args[0]
        self.assertEqual(
            GrentonCluApiActionRequest.from_action(action).payload,
            "Evening()",
        )
        self.assertEqual(action.clu_id, "clu1")
        self.assertEqual(status, 0)
        self.assertIn("✓ Evening()", output)

    def test_script_execution_with_arguments(self):
        api, status, output = self.run_command(
            base_args=[
                "--clu",
                "Main",
                "--object",
                "Evening",
                "--call-type",
                "script",
                "--value",
                '"room A", 42',
            ]
        )
        action = api.execute_action.await_args.args[0]
        self.assertEqual(
            GrentonCluApiActionRequest.from_action(action).payload,
            'Evening("room A", 42)',
        )
        self.assertEqual(status, 0)
        self.assertIn('✓ Evening("room A", 42)', output)

    def test_rejected_script_call_exits_unsuccessfully(self):
        api, status, output = self.run_command(
            base_args=["--clu", "Main", "--object", "Evening", "--call-type", "script"],
            result=False,
        )
        api.execute_action.assert_awaited_once()
        self.assertEqual(status, 1)
        self.assertEqual(output, "")

    def test_script_exception_still_disconnects_and_exits_unsuccessfully(self):
        api, status, output = self.run_command(
            base_args=["--clu", "Main", "--object", "Evening", "--call-type", "script"],
            error=TimeoutError("No response"),
        )
        api.execute_action.assert_awaited_once()
        self.assertEqual(status, 1)
        self.assertEqual(output, "")

    def test_method_requires_action_index(self):
        _, status, _ = self.run_command(
            base_args=["--clu", "Main", "--object", "THERMOSTAT1", "--value", "10"]
        )
        self.assertEqual(status, 2)

    def test_method_requires_value(self):
        _, status, _ = self.run_command(
            base_args=["--clu", "Main", "--object", "THERMOSTAT1", "--action", "10"]
        )
        self.assertEqual(status, 2)

    def test_attribute_requires_action_index(self):
        _, status, _ = self.run_command(
            base_args=[
                "--clu",
                "Main",
                "--object",
                "THERMOSTAT1",
                "--call-type",
                "attribute",
                "--value",
                "10",
            ]
        )
        self.assertEqual(status, 2)

    def test_attribute_requires_value(self):
        _, status, _ = self.run_command(
            base_args=[
                "--clu",
                "Main",
                "--object",
                "THERMOSTAT1",
                "--call-type",
                "attribute",
                "--action",
                "10",
            ]
        )
        self.assertEqual(status, 2)

    def test_rejected_attribute_call_exits_unsuccessfully(self):
        api, status, output = self.run_command(
            ("--call-type", "attribute"), result=False
        )
        api.execute_action.assert_awaited_once()
        self.assertEqual(status, 1)
        self.assertEqual(output, "")

    def test_attribute_exception_still_disconnects_and_exits_unsuccessfully(self):
        api, status, output = self.run_command(
            ("--call-type", "attribute"), error=TimeoutError("No response")
        )
        api.execute_action.assert_awaited_once()
        self.assertEqual(status, 1)
        self.assertEqual(output, "")

    def test_connection_failure_never_sends_an_action(self):
        api, status, _ = self.run_command(("--call-type", "attribute"), connected=False)
        api.execute_action.assert_not_awaited()
        self.assertEqual(status, 1)

    def test_invalid_call_type_is_rejected_by_argument_parser(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as exit_result,
        ):
            create_parser().parse_args(
                [
                    "execute",
                    "--object",
                    "THERMOSTAT1",
                    "--action",
                    "10",
                    "--value",
                    "10",
                    "--call-type",
                    "unknown",
                ]
            )
        self.assertEqual(exit_result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
