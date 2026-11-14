"""Management request framing, binary replies and export cleanup without a CLU."""

import asyncio
import contextlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import commands  # noqa: F401 - initializes the CLI import path
from clu_management import fetch_report
from commands import create_parser
from commands.diagnostics import format_report
from grenton_api import GrentonManager


class ManagementReportTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, kind="diagnostics", *, reply=True, extra_replies=()):
        clu = SimpleNamespace(
            ip="192.0.2.10", port=1234, serial_number="221003443",
            id="CLU828599", name="Main",
        )
        report = {"sn": 221003443, "fwVer": "5.15.24-2635"}
        if kind == "diagnostics":
            report.update(tfbusDiagnostic=[{"sn": 261005031, "fails": 428}], zwaveDiagnostic=[])
        else:
            report.update(tfbusDevices=[{"sn": 261005031, "fwVer": "1.2.7"}], zwaveDevices=[])
        fixture = SimpleNamespace(clu=clu, report=report, sent=[], protocol=None)
        transport = SimpleNamespace(
            close=Mock(), is_closing=Mock(return_value=False),
            get_extra_info=Mock(return_value=(clu.ip, clu.port)),
        )

        def sendto(payload, *args):
            fixture.sent.append(payload)
            if not reply:
                return
            peer = (clu.ip, clu.port)
            for message, address in extra_replies:
                fixture.protocol.datagram_received(message, address)
            if payload.startswith(b"req_diagnostic_refresh:"):
                message_id = payload.rsplit(b":", 1)[1]
                response = b"resp_diagnostic_refresh:" + message_id + b":SUCCESS"
            elif payload == b"req_refresh_config":
                response = b"resp_refresh_config:192.0.2.10:00000000:\xf8\xff"
            elif payload == b"req_start_ftp":
                response = b"resp:OK"
            else:
                raise AssertionError(f"Unexpected management payload {payload!r}")
            fixture.protocol.datagram_received(response, peer)

        transport.sendto = Mock(side_effect=sendto)
        fixture.transport = transport

        async def endpoint(factory, **kwargs):
            fixture.protocol = factory()
            fixture.protocol.connection_made(transport)
            return transport, fixture.protocol

        fixture.endpoint = endpoint
        fixture.cipher = SimpleNamespace(encrypt=lambda value: value, decrypt=lambda value: value)
        return fixture

    async def execute(self, fixture, kind="diagnostics", *, contents=None, error=None, timeout=0.1):
        if contents is None:
            contents = json.dumps(fixture.report).encode()
        tftp = AsyncMock(return_value=contents, side_effect=error)
        with (
            patch("homeassistant_grenton.domain.api.clu_files.GrentonCipher", return_value=fixture.cipher),
            patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=fixture.endpoint),
            patch("homeassistant_grenton.domain.api.clu_files.read_tftp", tftp),
        ):
            result = await fetch_report(fixture.clu, object(), kind, timeout=timeout)
        return result, tftp

    async def test_diagnostics_uses_target_ip_and_matching_id_before_read(self):
        fixture = self.fixture()
        result, tftp = await self.execute(fixture)
        self.assertEqual(result, fixture.report)
        self.assertRegex(fixture.sent[0], rb"^req_diagnostic_refresh:192\.0\.2\.10:[0-9a-f]{8}$")
        self.assertEqual(fixture.sent[1:], [b"req_start_ftp"])
        self.assertEqual(tftp.await_args.args[:2], ("192.0.2.10", "m:\\DIAGNOSTIC_PACK.JSON"))
        fixture.transport.close.assert_called_once()

    async def test_firmware_accepts_binary_refresh_reply_and_downloads_inventory(self):
        fixture = self.fixture("firmware")
        result, tftp = await self.execute(fixture, "firmware")
        self.assertEqual(result, fixture.report)
        self.assertEqual(fixture.sent, [b"req_refresh_config", b"req_start_ftp"])
        self.assertEqual(tftp.await_args.args[:2], ("192.0.2.10", "a:\\CONFIG.JSON"))
        fixture.transport.close.assert_called_once()

    async def test_wrong_peer_and_unmatched_response_do_not_satisfy_refresh(self):
        fixture = self.fixture(extra_replies=(
            (b"resp_diagnostic_refresh:00000000:SUCCESS", ("192.0.2.99", 1234)),
            (b"resp_diagnostic_refresh:ffffffff:SUCCESS", ("192.0.2.10", 1234)),
            (b"resp:unrelated", ("192.0.2.10", 1234)),
        ))
        result, _ = await self.execute(fixture)
        self.assertEqual(result, fixture.report)
        fixture.transport.close.assert_called_once()

    async def test_unanswered_request_times_out_and_never_downloads(self):
        fixture = self.fixture(reply=False)
        with (
            patch("homeassistant_grenton.domain.api.clu_files.GrentonCipher", return_value=fixture.cipher),
            patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=fixture.endpoint),
            patch("homeassistant_grenton.domain.api.clu_files.read_tftp", AsyncMock()) as tftp,
            self.assertRaises(TimeoutError),
        ):
            await fetch_report(fixture.clu, object(), "diagnostics", timeout=0.01)
        tftp.assert_not_awaited()
        fixture.transport.close.assert_called_once()

    async def test_unrelated_or_spoofed_replies_alone_cannot_trigger_download(self):
        fixture = self.fixture(reply=False)

        def unrelated(payload, *args):
            message_id = payload.rsplit(b":", 1)[1]
            fixture.protocol.datagram_received(
                b"resp_diagnostic_refresh:" + message_id + b":SUCCESS", ("192.0.2.99", 1234)
            )
            fixture.protocol.datagram_received(
                b"resp_diagnostic_refresh:ffffffff:SUCCESS", (fixture.clu.ip, 1234)
            )

        fixture.transport.sendto.side_effect = unrelated
        with (
            patch("homeassistant_grenton.domain.api.clu_files.GrentonCipher", return_value=fixture.cipher),
            patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=fixture.endpoint),
            patch("homeassistant_grenton.domain.api.clu_files.read_tftp", AsyncMock()) as tftp,
            self.assertRaises(TimeoutError),
        ):
            await fetch_report(fixture.clu, object(), "diagnostics", timeout=0.01)
        tftp.assert_not_awaited()
        fixture.transport.close.assert_called_once()

    async def test_cancellation_while_waiting_for_refresh_closes_udp(self):
        fixture = self.fixture(reply=False)
        sent = asyncio.Event()
        fixture.transport.sendto.side_effect = lambda *args: sent.set()
        with (
            patch("homeassistant_grenton.domain.api.clu_files.GrentonCipher", return_value=fixture.cipher),
            patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=fixture.endpoint),
            patch("homeassistant_grenton.domain.api.clu_files.read_tftp", AsyncMock()) as tftp,
        ):
            task = asyncio.create_task(fetch_report(fixture.clu, object(), "diagnostics"))
            await sent.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        tftp.assert_not_awaited()
        fixture.transport.close.assert_called_once()

    async def test_download_failure_or_cancellation_releases_management_socket(self):
        for error in (ConnectionError("TFTP failed"), asyncio.CancelledError()):
            fixture = self.fixture()
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                await self.execute(fixture, error=error)
            fixture.transport.close.assert_called_once()

    async def test_malformed_or_incomplete_exports_are_rejected_after_cleanup(self):
        invalid = [
            b"not-json", b"[]", b"null",
            b'{"sn":221003443,"fwVer":"1","tfbusDiagnostic":[],"zwaveDiagnostic":null}',
            b'{"sn":221003443,"tfbusDiagnostic":[],"zwaveDiagnostic":[]}',
            b'{"sn":123,"fwVer":"1","tfbusDiagnostic":[],"zwaveDiagnostic":[]}',
        ]
        for contents in invalid:
            fixture = self.fixture()
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                await self.execute(fixture, contents=contents)
            fixture.transport.close.assert_called_once()


class ReportFormattingTests(unittest.TestCase):
    def report(self, kind="diagnostics"):
        report = {
            "sn": 221003443, "fwVer": "5.15.24-2635", "status": "LOGGING",
            "voltage": 24.2, "cloudConn": False,
            "vendorExtra": {"value": "żółć"},
        }
        device = {
            "sn": 261005031, "fwVer": "1.2.7", "status": "OK",
            "voltage": 0, "fails": 428, "banned": False,
        }
        if kind == "diagnostics":
            report.update(tfbusDiagnostic=[device], zwaveDiagnostic=[])
        else:
            report.update(tfbusDevices=[device], zwaveDevices=[])
        return report

    def test_json_preserves_full_export_with_unknown_fields_and_unicode(self):
        for kind in ("diagnostics", "firmware"):
            report = self.report(kind)
            self.assertEqual(json.loads(format_report(report, kind, "json")), report)
            self.assertIn("żółć", format_report(report, kind, "json"))

    def test_diagnostic_table_includes_clu_device_failures_and_zero_voltage(self):
        output = format_report(self.report(), "diagnostics", "text")
        self.assertIn("221003443", output)
        self.assertIn("261005031", output)
        self.assertIn("5.15.24-2635", output)
        self.assertIn("1.2.7", output)
        self.assertIn("428", output)
        self.assertIn("False", output)
        self.assertIn("TF-bus devices: 1", output)
        self.assertIn("Z-Wave devices: 0", output)
        self.assertIn("Cloud connected: False", output)
        self.assertIn("time window is unspecified", output)
        device_line = next(line for line in output.splitlines() if "261005031" in line)
        self.assertIn("0", device_line.split())

    def test_firmware_table_is_inventory_without_failure_counter_claims(self):
        output = format_report(self.report("firmware"), "firmware", "text")
        self.assertIn("1.2.7", output)
        self.assertIn("TF-bus devices: 1", output)
        self.assertNotIn("Fails", output)
        self.assertNotIn("428", output)

    def test_commands_dispatch_selected_report_and_json_output(self):
        for kind in ("diagnostics", "firmware"):
            report = self.report(kind)
            args = create_parser().parse_args([
                kind, "--clu", "Main", "--config", "/tmp/example.yaml",
                "--output", "json", "--timeout", "12.5",
                "--skip-firmware-check",
            ])
            manager = SimpleNamespace(diagnostics=AsyncMock(return_value=report), firmware=AsyncMock(return_value=report))
            output = io.StringIO()
            with (
                patch("commands.diagnostics.GrentonManager", return_value=manager),
                contextlib.redirect_stdout(output),
            ):
                args.handler(args)
            self.assertEqual(json.loads(output.getvalue()), report)
            getattr(manager, kind).assert_awaited_once_with(Path("/tmp/example.yaml"), "Main", timeout=12.5)
            other = "firmware" if kind == "diagnostics" else "diagnostics"
            getattr(manager, other).assert_not_awaited()

    def test_invalid_deadlines_are_rejected_before_network_access(self):
        for kind in ("diagnostics", "firmware"):
            for value in ("0", "-1", "301", "nan", "inf", "oops"):
                with (
                    self.subTest(kind=kind, value=value),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as result,
                ):
                    create_parser().parse_args([kind, "--timeout", value])
                self.assertEqual(result.exception.code, 2)

    def test_defaults_and_format_alias(self):
        for kind in ("diagnostics", "firmware"):
            args = create_parser().parse_args([kind])
            self.assertEqual((args.report_kind, args.output, args.timeout), (kind, "text", 10))
            self.assertEqual(create_parser().parse_args([kind, "--format", "json"]).output, "json")


class ReportManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_errors_exit_with_failure_instead_of_partial_output(self):
        for kind in ("diagnostics", "firmware"):
            for error in (TimeoutError("expired"), ValueError("bad export")):
                clu = SimpleNamespace(name="Main")
                config = SimpleNamespace(clus=[clu], encryption=object())
                with (
                    self.subTest(kind=kind, error=type(error).__name__),
                    patch("grenton_api.load_configuration", return_value=config),
                    patch("grenton_api.fetch_report", AsyncMock(side_effect=error)),
                    self.assertRaises(SystemExit) as result,
                ):
                    await getattr(GrentonManager(), kind)(Path("unused"), "Main")
                self.assertEqual(result.exception.code, 1)

    async def test_unknown_clu_never_starts_management_request(self):
        config = SimpleNamespace(clus=[SimpleNamespace(name="Main")], encryption=object())
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.fetch_report", AsyncMock()) as request,
            self.assertRaises(SystemExit) as result,
        ):
            await GrentonManager().diagnostics(Path("unused"), "Missing")
        self.assertEqual(result.exception.code, 1)
        request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
