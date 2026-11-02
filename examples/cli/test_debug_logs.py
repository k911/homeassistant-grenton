"""Captured Telnet framing and debug-log lifecycle without a live CLU."""

import asyncio
import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import commands  # noqa: F401 - initializes the CLI import path
from clu_logs import (
    INITIAL_NEGOTIATION,
    LogOutputClosed,
    TelnetDecoder,
    TelnetSessionRejected,
    stream_logs,
)
from commands import create_parser
from grenton_api import GrentonManager
from homeassistant_grenton.domain.api.clu_messages.action import (
    GrentonCluApiActionRequest,
)


class TelnetDecoderTests(unittest.TestCase):
    def test_captured_negotiation_survives_one_byte_tcp_fragments(self):
        decoder = TelnetDecoder()
        data = bytes.fromhex("ff fb 03 ff fd 03 ff fb 01 ff fb 00 ff fd 00 ff fd 00")
        text, replies = bytearray(), bytearray()
        for value in data:
            chunk, reply = decoder.feed(bytes([value]))
            text.extend(chunk)
            replies.extend(reply)
        self.assertEqual(INITIAL_NEGOTIATION, bytes.fromhex("ff fb 00"))
        self.assertEqual(text, b"")
        self.assertEqual(replies, bytes.fromhex("ff fe 03 ff fc 03 ff fe 01 ff fe 00"))

    def test_subnegotiation_is_removed_and_escaped_iac_is_preserved(self):
        decoder = TelnetDecoder()
        text = bytearray()
        # Includes an escaped IAC inside subnegotiation, then a split IAC SE.
        chunks = [b"before\xff", b"\xfa\x18terminal\xff\xffvalue\xff", b"\xf0after\xff", b"\xff!\r\n"]
        for chunk in chunks:
            payload, reply = decoder.feed(chunk)
            text.extend(payload)
            self.assertEqual(reply, b"")
        self.assertEqual(text, b"beforeafter\xff!\r\n")

    def test_unsupported_options_are_refused_without_leaking_into_logs(self):
        decoder = TelnetDecoder()
        text, reply = decoder.feed(bytes.fromhex("ff fb 18 ff fd 1f ff fc 18 ff fe 1f") + b"log")
        self.assertEqual(text, b"log")
        self.assertEqual(reply, bytes.fromhex("ff fe 18 ff fc 1f"))


class StreamLogsTests(unittest.IsolatedAsyncioTestCase):
    def writer(self):
        return SimpleNamespace(
            write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock()
        )

    async def test_stream_decodes_split_utf8_and_closes_on_eof(self):
        writer = self.writer()
        chunks = iter([b"\xff\xfb\x01", b"[DEBUG] \xc5", b"\xbc\xc3\xb3\xc5\x82\xc4\x87\r\n", b""])

        async def read(_size):
            # Give the CRLF task an opportunity to run while receiving data.
            await asyncio.sleep(0)
            return next(chunks)

        reader = SimpleNamespace(read=AsyncMock(side_effect=read))
        output = io.StringIO()
        with (
            patch("clu_logs.asyncio.open_connection", AsyncMock(return_value=(reader, writer))) as connect,
            contextlib.redirect_stdout(output),
        ):
            await stream_logs("192.0.2.10", 23)
        connect.assert_awaited_once_with("192.0.2.10", 23)
        self.assertEqual(output.getvalue(), "[DEBUG] żółć\r\n")
        writes = [call.args[0] for call in writer.write.call_args_list]
        self.assertEqual(writes[0], INITIAL_NEGOTIATION)
        self.assertIn(b"\r\n", writes)
        self.assertIn(bytes.fromhex("ff fe 01"), writes)
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    async def test_cancelled_stream_releases_tcp_socket(self):
        writer = self.writer()
        reader = SimpleNamespace(read=AsyncMock(side_effect=asyncio.CancelledError))
        with (
            patch("clu_logs.asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
            self.assertRaises(asyncio.CancelledError),
        ):
            await stream_logs("192.0.2.10")
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    async def test_reset_during_read_releases_socket_before_propagating(self):
        writer = self.writer()
        reader = SimpleNamespace(read=AsyncMock(side_effect=ConnectionResetError("reset")))
        with (
            patch("clu_logs.asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
            self.assertRaises(TelnetSessionRejected),
        ):
            await stream_logs("192.0.2.10")
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    async def test_silent_or_closed_unnegotiated_session_is_rejected(self):
        for silent in (False, True):
            with self.subTest(silent=silent):
                writer = self.writer()

                async def read(_size, silent=silent):
                    if silent:
                        await asyncio.Future()
                    return b""

                reader = SimpleNamespace(read=AsyncMock(side_effect=read))
                with (
                    patch("clu_logs.asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
                    patch("clu_logs.CONNECT_TIMEOUT", 0.01),
                    self.assertRaises(TelnetSessionRejected),
                ):
                    await stream_logs("192.0.2.10")
                writer.close.assert_called_once()
                writer.wait_closed.assert_awaited_once()

    async def test_reset_after_negotiation_remains_a_reconnectable_error(self):
        writer = self.writer()
        reader = SimpleNamespace(read=AsyncMock(side_effect=[b"\xff\xfd\x00", ConnectionResetError("reboot")]))
        with (
            patch("clu_logs.asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
            self.assertRaises(ConnectionResetError),
        ):
            await stream_logs("192.0.2.10")
        writer.close.assert_called_once()

    async def test_failed_keepalive_cancels_blocked_receiver_and_closes(self):
        writer = self.writer()
        writer.drain.side_effect = [None, BrokenPipeError("keepalive failed")]
        cancelled = asyncio.Event()
        negotiated = False

        async def read(_size):
            nonlocal negotiated
            if not negotiated:
                negotiated = True
                return b"\xff\xfd\x00"
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        reader = SimpleNamespace(read=AsyncMock(side_effect=read))
        with (
            patch("clu_logs.asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
            self.assertRaises(BrokenPipeError),
        ):
            await stream_logs("192.0.2.10")
        self.assertTrue(cancelled.is_set())
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    async def test_closed_stdout_is_distinct_from_a_network_failure(self):
        writer = self.writer()
        reader = SimpleNamespace(read=AsyncMock(return_value=b"log\r\n"))
        output = Mock()
        output.write.side_effect = BrokenPipeError("consumer exited")
        with (
            patch("clu_logs.asyncio.open_connection", AsyncMock(return_value=(reader, writer))),
            patch("clu_logs.sys.stdout", output),
            self.assertRaises(LogOutputClosed),
        ):
            await stream_logs("192.0.2.10")
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()


class DebugLogsManagerTests(unittest.IsolatedAsyncioTestCase):
    def fixtures(self):
        target = SimpleNamespace(id="CLU_REAL", object_name="CLU_REAL", name="Main", ip="192.0.2.10")
        config = SimpleNamespace(clus=[target], encryption=object())
        api = SimpleNamespace(
            connect=AsyncMock(return_value=True),
            execute_action=AsyncMock(return_value=True),
            disconnect=AsyncMock(),
        )
        return config, api

    async def test_levels_use_numeric_lua_attribute_values(self):
        for numeric, level in enumerate(("OFF", "ERROR", "WARNING", "INFO", "DEBUG")):
            with self.subTest(level=level):
                config, api = self.fixtures()
                stream = AsyncMock(side_effect=asyncio.CancelledError)
                with (
                    patch("grenton_api.load_configuration", return_value=config),
                    patch("grenton_api.GrentonCluApi", return_value=api),
                    patch("grenton_api.stream_logs", stream),
                ):
                    if level == "OFF":
                        await GrentonManager().debug_logs(Path("unused"), "Main", level)
                        stream.assert_not_awaited()
                    else:
                        with self.assertRaises(asyncio.CancelledError):
                            await GrentonManager().debug_logs(Path("unused"), "Main", level, 2323)
                        stream.assert_awaited_once_with("192.0.2.10", 2323)
                action = api.execute_action.await_args.args[0]
                self.assertEqual(action.clu_id, "CLU_REAL")
                self.assertEqual(
                    GrentonCluApiActionRequest.from_action(action).payload,
                    f"CLU_REAL:set(31,{numeric})",
                )
                api.disconnect.assert_awaited_once()

    async def test_reconnect_reapplies_level_after_eof_or_reset(self):
        for first_result in (None, ConnectionResetError("reboot")):
            with self.subTest(first_result=first_result):
                config, api = self.fixtures()
                stream = AsyncMock(side_effect=[first_result, asyncio.CancelledError])
                with (
                    patch("grenton_api.load_configuration", return_value=config),
                    patch("grenton_api.GrentonCluApi", return_value=api),
                    patch("grenton_api.stream_logs", stream),
                    patch("grenton_api.asyncio.sleep", AsyncMock()) as sleep,
                    self.assertRaises(asyncio.CancelledError),
                ):
                    await GrentonManager().debug_logs(Path("unused"), "Main")
                self.assertEqual(stream.await_count, 2)
                self.assertGreaterEqual(api.execute_action.await_count, 2)
                sleep.assert_awaited()
                api.disconnect.assert_awaited_once()

    async def test_initial_set_failure_does_not_open_telnet(self):
        config, api = self.fixtures()
        api.execute_action.return_value = False
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            patch("grenton_api.stream_logs", AsyncMock()) as stream,
            self.assertRaises(SystemExit) as result,
        ):
            await GrentonManager().debug_logs(Path("unused"), "Main")
        self.assertEqual(result.exception.code, 1)
        stream.assert_not_awaited()
        api.disconnect.assert_awaited_once()

    async def test_initial_rejected_session_warns_and_exits_without_retrying(self):
        config, api = self.fixtures()
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            patch("grenton_api.stream_logs", AsyncMock(side_effect=TelnetSessionRejected("reset by peer"))) as stream,
            patch("grenton_api.asyncio.sleep", AsyncMock()) as sleep,
            self.assertLogs("grenton_api", level="WARNING") as logs,
            self.assertRaises(SystemExit) as result,
        ):
            await GrentonManager().debug_logs(Path("unused"), "Main")
        self.assertEqual(result.exception.code, 1)
        self.assertIn("Only one Telnet", " ".join(logs.output))
        self.assertIn("Stop logging in Object Manager", " ".join(logs.output))
        stream.assert_awaited_once()
        sleep.assert_not_awaited()
        api.execute_action.assert_awaited_once()
        api.disconnect.assert_awaited_once()

    async def test_startup_rejections_after_an_established_session_still_retry(self):
        config, api = self.fixtures()
        stream = AsyncMock(side_effect=[ConnectionResetError("reboot"), TelnetSessionRejected("starting"), asyncio.CancelledError])
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            patch("grenton_api.stream_logs", stream),
            patch("grenton_api.asyncio.sleep", AsyncMock()),
            self.assertRaises(asyncio.CancelledError),
        ):
            await GrentonManager().debug_logs(Path("unused"), "Main")
        self.assertEqual(stream.await_count, 3)
        api.disconnect.assert_awaited_once()

    async def test_closed_output_stops_without_reconnecting(self):
        config, api = self.fixtures()
        with (
            patch("grenton_api.load_configuration", return_value=config),
            patch("grenton_api.GrentonCluApi", return_value=api),
            patch("grenton_api.stream_logs", AsyncMock(side_effect=LogOutputClosed)) as stream,
            patch("grenton_api.asyncio.sleep", AsyncMock()) as sleep,
            self.assertRaises(LogOutputClosed),
        ):
            await GrentonManager().debug_logs(Path("unused"), "Main")
        stream.assert_awaited_once()
        sleep.assert_not_awaited()
        api.disconnect.assert_awaited_once()


class DebugLogsParserTests(unittest.TestCase):
    def test_debug_defaults_and_case_insensitive_levels(self):
        parser = create_parser()
        args = parser.parse_args(["debug-logs"])
        self.assertEqual(args.level, "DEBUG")
        self.assertEqual(args.port, 23)
        self.assertEqual(parser.parse_args(["logs", "--level", "warning"]).level, "WARNING")

    def test_invalid_level_or_port_is_rejected(self):
        for extra in (("--level", "TRACE"), ("--port", "0"), ("--port", "65536"), ("--port", "word")):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                create_parser().parse_args(["debug-logs", *extra])
            self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
