"""CLU TFTP reads with mocked datagrams; no live network access."""

import asyncio
import struct
import unittest
from unittest.mock import Mock, patch

from clu_tftp import TftpError, read_tftp


def data(block, body):
    return struct.pack("!HH", 3, block) + body


class TftpTests(unittest.IsolatedAsyncioTestCase):
    def endpoint(self, on_send):
        peer = ("192.0.2.10", 69)
        transport = Mock()
        transport.get_extra_info.return_value = peer
        protocols = []

        async def create(factory, **kwargs):
            self.assertEqual(kwargs, {"remote_addr": peer})
            protocol = factory()
            protocols.append(protocol)
            protocol.connection_made(transport)
            transport.sendto.side_effect = lambda value: on_send(protocol, value, peer)
            return transport, protocol

        return create, transport, protocols

    async def test_multiblock_duplicate_and_out_of_order_do_not_duplicate_bytes(self):
        sent = []

        def respond(protocol, request, peer):
            sent.append(request)
            if len(sent) == 1:
                protocol.datagram_received(data(1, b"a" * 512), peer)
                protocol.datagram_received(data(1, b"a" * 512), peer)
                protocol.datagram_received(data(3, b"future"), peer)
                protocol.datagram_received(data(2, b"tail\r\n"), peer)

        create, transport, _ = self.endpoint(respond)
        with patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", create):
            result = await read_tftp("192.0.2.10", "a:\\CONFIG.JSON")
        self.assertEqual(result, b"a" * 512 + b"tail\r\n")
        self.assertEqual(sent[0], b"\0\1a:\\CONFIG.JSON\0netascii\0")
        self.assertEqual(sent[-1], b"\0\4\0\2")
        self.assertEqual(sent.count(b"\0\4\0\1"), 3)
        transport.close.assert_called_once()

    async def test_lost_rrq_and_ack_retransmit_without_leaking(self):
        rrqs = 0
        acks = 0

        def respond(protocol, request, peer):
            nonlocal rrqs, acks
            if request.startswith(b"\0\1"):
                rrqs += 1
                if rrqs == 2:
                    protocol.datagram_received(data(1, b"a" * 512), peer)
            elif request == b"\0\4\0\1":
                acks += 1
                if acks == 2:
                    protocol.datagram_received(data(2, b""), peer)

        create, transport, _ = self.endpoint(respond)
        with patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", create):
            result = await read_tftp("192.0.2.10", "a:\\CONFIG.JSON", timeout=0.001)
        self.assertEqual(result, b"a" * 512)
        self.assertEqual((rrqs, acks), (2, 2))
        transport.close.assert_called_once()

    async def test_unanswered_request_has_a_finite_retry_budget(self):
        create, transport, _ = self.endpoint(lambda *_: None)
        with (
            patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", create),
            self.assertRaisesRegex(TimeoutError, "block 1"),
        ):
            await read_tftp("192.0.2.10", "a:\\CONFIG.JSON", timeout=0.001, retries=2)
        self.assertEqual(transport.sendto.call_count, 3)
        transport.close.assert_called_once()

    async def test_wrong_peer_is_ignored_and_correct_peer_is_accepted(self):
        def respond(protocol, request, peer):
            if request.startswith(b"\0\1"):
                protocol.datagram_received(data(1, b"wrong"), (peer[0], 12345))
                protocol.datagram_received(data(1, b"correct"), peer)

        create, transport, _ = self.endpoint(respond)
        with patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", create):
            result = await read_tftp("192.0.2.10", "a:\\CONFIG.JSON")
        self.assertEqual(result, b"correct")
        transport.close.assert_called_once()

    async def test_protocol_errors_and_size_limit_close_the_socket(self):
        for packet, expected in (
            (b"\0\3\0", "Truncated"),
            (b"\0\5\0\4Illegal Operation\0", "TFTP error 4: Illegal Operation"),
            (b"\0\5\0\4bad", "Malformed"),
            (b"\0\4\0\1", "Unexpected"),
            (data(1, b"a" * 513), "Oversized"),
            (data(1, b"a" * 5), "maximum size"),
        ):
            with self.subTest(packet=packet[:20]):
                def respond(protocol, request, peer, packet=packet):
                    if request.startswith(b"\0\1"):
                        protocol.datagram_received(packet, peer)

                create, transport, _ = self.endpoint(respond)
                with (
                    patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", create),
                    self.assertRaisesRegex(TftpError, expected),
                ):
                    await read_tftp("192.0.2.10", "a:\\CONFIG.JSON", max_bytes=4)
                transport.close.assert_called_once()

    async def test_cancellation_closes_socket(self):
        started = asyncio.Event()
        create, transport, _ = self.endpoint(lambda *_: started.set())
        with patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", create):
            task = asyncio.create_task(read_tftp("192.0.2.10", "a:\\CONFIG.JSON"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        transport.close.assert_called_once()

    async def test_cancel_during_endpoint_creation_closes_late_owned_socket(self):
        transport = Mock()
        made = asyncio.Event()

        async def create(factory, **_kwargs):
            factory().connection_made(transport)
            made.set()
            await asyncio.Future()

        with patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", create):
            task = asyncio.create_task(read_tftp("192.0.2.10", "a:\\CONFIG.JSON"))
            await made.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        transport.close.assert_called_once()

    async def test_udp_transport_error_is_propagated_and_closed(self):
        def respond(protocol, _request, _peer):
            protocol.error_received(ConnectionRefusedError("UDP service unavailable"))

        create, transport, _ = self.endpoint(respond)
        with (
            patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", create),
            self.assertRaises(ConnectionRefusedError),
        ):
            await read_tftp("192.0.2.10", "a:\\CONFIG.JSON")
        transport.close.assert_called_once()

    async def test_duplicate_noise_cannot_prevent_overall_timeout(self):
        def respond(protocol, request, peer):
            if request.startswith(b"\0\1"):
                protocol.datagram_received(data(1, b"a" * 512), peer)
            elif request == b"\0\4\0\1":
                # Simulate a peer repeatedly sending an old block in response.
                asyncio.get_running_loop().call_soon(
                    protocol.datagram_received, data(1, b"a" * 512), peer,
                )

        create, transport, _ = self.endpoint(respond)
        with (
            patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", create),
            patch("homeassistant_grenton.domain.api.tftp.TRANSFER_TIMEOUT", 0.01),
            self.assertRaises(TimeoutError),
        ):
            await read_tftp("192.0.2.10", "a:\\CONFIG.JSON", timeout=1)
        transport.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
