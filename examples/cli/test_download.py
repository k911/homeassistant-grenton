"""CLU file retrieval and atomic local saves without contacting a device."""

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import commands  # noqa: F401 - initializes the CLI import path
import test_clu_management as management_tests
from clu_management import download_file
from commands import create_parser
from grenton_api import GrentonManager


class DownloadProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, fixture, *, payload=b"\0\xff\r\n", error=None):
        tftp = AsyncMock(return_value=payload, side_effect=error)
        with (
            patch("homeassistant_grenton.domain.api.clu_files.GrentonCipher", return_value=fixture.cipher),
            patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=fixture.endpoint),
            patch("homeassistant_grenton.domain.api.clu_files.read_tftp", tftp),
        ):
            result = await download_file(
                fixture.clu, object(), r"a:\USER.LUA", timeout=0.1, max_bytes=1024,
            )
        return result, tftp

    async def test_generic_download_enables_tftp_without_refreshing_or_parsing(self):
        fixture = management_tests.ManagementReportTests().fixture()
        result, tftp = await self.execute(fixture)
        self.assertEqual(result, b"\0\xff\r\n")
        self.assertEqual(fixture.sent, [b"req_start_ftp"])
        tftp.assert_awaited_once_with(
            fixture.clu.ip, r"a:\USER.LUA", timeout=0.1, max_bytes=1024,
        )
        fixture.transport.close.assert_called_once()

    async def test_download_error_or_cancellation_releases_management_socket(self):
        for error in (ConnectionError("failed"), asyncio.CancelledError()):
            fixture = management_tests.ManagementReportTests().fixture()
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                await self.execute(fixture, error=error)
            fixture.transport.close.assert_called_once()

    async def test_invalid_remote_path_is_rejected_before_opening_a_socket(self):
        fixture = management_tests.ManagementReportTests().fixture()
        for path in ("", "a:\\bad\0path", "a:\\żółć"):
            with (
                self.subTest(path=path),
                patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", AsyncMock()) as endpoint,
                self.assertRaises(ValueError),
            ):
                await download_file(fixture.clu, object(), path)
            endpoint.assert_not_awaited()

    async def test_start_service_timeout_does_not_start_file_read(self):
        fixture = management_tests.ManagementReportTests().fixture(reply=False)
        with (
            patch("homeassistant_grenton.domain.api.clu_files.GrentonCipher", return_value=fixture.cipher),
            patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=fixture.endpoint),
            patch("homeassistant_grenton.domain.api.clu_files.read_tftp", AsyncMock()) as tftp,
            self.assertRaises(TimeoutError),
        ):
            await download_file(fixture.clu, object(), r"a:\USER.LUA", timeout=0.01)
        tftp.assert_not_awaited()
        fixture.transport.close.assert_called_once()


class DownloadCommandTests(unittest.TestCase):
    def args(self, destination: Path, *extra):
        return create_parser().parse_args([
            "download", "--clu", "Main", "--remote", r"a:\USER.LUA",
            "--output", str(destination), *extra,
        ])

    def run_command(self, args, download):
        with patch("commands.download.GrentonManager.download_file", download):
            args.handler(args)

    def test_binary_download_is_saved_exactly_and_default_options_are_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "user.lua"
            contents = b"\0\xffprint('test')\r\n"
            download = AsyncMock(return_value=contents)
            self.run_command(self.args(destination), download)
            self.assertEqual(destination.read_bytes(), contents)
            self.assertEqual(list(Path(directory).iterdir()), [destination])
            self.assertEqual(download.await_args.args[1:], ("Main", r"a:\USER.LUA"))
            self.assertEqual(download.await_args.kwargs, {"timeout": 10, "max_bytes": 4 * 1024 * 1024})

    def test_existing_file_requires_force_before_contacting_clu(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "user.lua"
            destination.write_bytes(b"keep")
            download = AsyncMock(return_value=b"replacement")
            with self.assertRaises(SystemExit) as result:
                self.run_command(self.args(destination), download)
            self.assertEqual(result.exception.code, 1)
            self.assertEqual(destination.read_bytes(), b"keep")
            download.assert_not_awaited()

    def test_force_replaces_existing_file_and_allows_an_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "debug.bin"
            destination.write_bytes(b"old")
            self.run_command(self.args(destination, "--force"), AsyncMock(return_value=b""))
            self.assertEqual(destination.read_bytes(), b"")
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_failed_or_cancelled_download_preserves_existing_file_without_temporary_residue(self):
        for error in (ConnectionError("transfer failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "user.lua"
                destination.write_bytes(b"keep")
                expected = SystemExit if isinstance(error, OSError) else type(error)
                with self.assertRaises(expected) as result:
                    self.run_command(self.args(destination, "--force"), AsyncMock(side_effect=error))
                if expected is SystemExit:
                    self.assertEqual(result.exception.code, 1)
                self.assertEqual(destination.read_bytes(), b"keep")
                self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_file_created_during_transfer_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "user.lua"

            async def download(*args, **kwargs):
                destination.write_bytes(b"created meanwhile")
                return b"remote bytes"

            with self.assertRaises(SystemExit) as result:
                self.run_command(self.args(destination), AsyncMock(side_effect=download))
            self.assertEqual(result.exception.code, 1)
            self.assertEqual(destination.read_bytes(), b"created meanwhile")
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_local_publish_failure_preserves_old_output_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "user.lua"
            destination.write_bytes(b"keep")
            with (
                patch("commands.download.os.replace", side_effect=PermissionError("publish failed")),
                self.assertRaises(SystemExit) as result,
            ):
                self.run_command(self.args(destination, "--force"), AsyncMock(return_value=b"new"))
            self.assertEqual(result.exception.code, 1)
            self.assertEqual(destination.read_bytes(), b"keep")
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_required_paths_and_invalid_limits_are_rejected_by_parser(self):
        cases = [
            ["download"],
            ["download", "--remote", r"a:\USER.LUA"],
            ["download", "--output", "user.lua"],
        ]
        base = ["download", "--remote", r"a:\USER.LUA", "--output", "user.lua"]
        cases.extend([*base, "--max-bytes", value] for value in ("0", "-1", "bad"))
        cases.extend([*base, "--timeout", value] for value in ("0", "nan", "inf"))
        for argv in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                create_parser().parse_args(argv)
            self.assertEqual(result.exception.code, 2)


class DownloadManagerTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        return SimpleNamespace(
            clus=[SimpleNamespace(id="CLU1", name="Main", ip="192.0.2.10")],
            encryption=object(),
        )

    async def test_manager_selects_clu_and_returns_original_bytes(self):
        config = self.config()
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.fetch_file", AsyncMock(return_value=b"\0\xff")) as fetch,
        ):
            result = await GrentonManager().download_file(
                Path("config.yaml"), "Main", r"a:\USER.LUA", timeout=2, max_bytes=100,
            )
        self.assertEqual(result, b"\0\xff")
        fetch.assert_awaited_once_with(
            config.clus[0], config.encryption, r"a:\USER.LUA", timeout=2, max_bytes=100,
        )

    async def test_unknown_clu_has_nonzero_exit_status_without_transfer(self):
        with (
            patch("grenton_api.load_configuration", return_value=self.config()),
            patch("grenton_api.fetch_file", AsyncMock()) as fetch,
            self.assertRaises(SystemExit) as result,
        ):
            await GrentonManager().download_file(Path("config.yaml"), "Missing", r"a:\USER.LUA")
        self.assertEqual(result.exception.code, 1)
        fetch.assert_not_awaited()

    async def test_transfer_error_has_nonzero_exit_status(self):
        with (
            patch("grenton_api.load_configuration", return_value=self.config()),
            patch("grenton_api.fetch_file", AsyncMock(side_effect=ConnectionError("failed"))),
            self.assertRaises(SystemExit) as result,
        ):
            await GrentonManager().download_file(Path("config.yaml"), "Main", r"a:\USER.LUA")
        self.assertEqual(result.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
