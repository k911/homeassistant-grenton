"""A busy subscription keeps data and keepalive acknowledgements independent."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_clu_lifecycle import fixture
from test_clu_protocol_lifecycle import socket_factory, tick

from custom_components.homeassistant_grenton.state import GrentonCluStateVariableKey


async def pending_ping(protocol, message_id):
    async def wait():
        while message_id not in protocol._pending:
            await tick()
        return protocol._pending[message_id]

    return await asyncio.wait_for(wait(), 1)


def report(protocol, api, value):
    protocol.datagram_received(
        f"resp:{api.clu.ip}:00000000:clientReport:0:{{{value}}}".encode(),
        (api.clu.ip, api.clu.port),
    )


def acknowledge(protocol, api, message_id):
    protocol.datagram_received(
        f"resp:{api.clu.ip}:{message_id}:pong".encode(),
        (api.clu.ip, api.clu.port),
    )


def test_reports_do_not_consume_ping_waiter_and_correct_ack_still_completes(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        with socket_factory(api, sockets):
            await api.connect()
            key = GrentonCluStateVariableKey("temperature")
            await api.register_component_states([key])
            endpoint = api._subscription_endpoints[0]
            protocol = endpoint.protocol
            endpoint.transport.reply = False
            api.on_subscription_report = AsyncMock()

            ping = asyncio.create_task(api.ping())
            waiter = await pending_ping(protocol, endpoint.keep_alive_id)
            received_before = protocol._received_replies
            for value in range(32):
                report(protocol, api, value)
                await tick()
                assert protocol._pending[endpoint.keep_alive_id] is waiter
                assert not waiter.done() and not ping.done()

            acknowledge(protocol, api, endpoint.keep_alive_id)
            assert await ping
            assert not protocol._pending
            assert protocol._received_replies == received_before + 1
            assert protocol._received_reports == 32
            assert api.on_subscription_report.await_count == 32
            assert api.on_subscription_report.await_args.args == ([key], [31])
            assert api.health.request_timeouts == 0
            assert not protocol._discarded_packets
            assert len(sockets) == 2

        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("finish", ["timeout", "cancel"])
def test_missing_ack_amid_reports_keeps_socket_and_ignores_late_ack_outside_wait(
    tmp_path, caplog, finish
):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        with socket_factory(api, sockets):
            await api.connect()
            await api.register_component_states([GrentonCluStateVariableKey("value")])
            endpoint = api._subscription_endpoints[0]
            protocol, transport = endpoint.protocol, endpoint.transport
            registration_id = endpoint.registration_id
            transport.reply = False
            protocol._response_timeout = 0.2
            api.on_subscription_report = AsyncMock()
            sent_before = [len(socket.sent) for socket in sockets]

            ping = asyncio.create_task(api.ping())
            waiter = await pending_ping(protocol, endpoint.keep_alive_id)
            for value in range(3):
                report(protocol, api, value)
                await tick()
            assert not waiter.done() and not ping.done()
            assert api.on_subscription_report.await_count == 3
            if finish == "timeout":
                assert not await ping
                assert api.health.request_timeouts == 1
                assert protocol.health.request_timeouts == 1
                message = next(
                    record.getMessage()
                    for record in caplog.records
                    if "Request timeout during ping" in record.getMessage()
                )
                assert "reports_during_wait=3" in message
                assert "other_replies_during_wait=0" in message
                assert "other_requests_in_flight_at_send=0" in message
            else:
                ping.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await ping
                assert api.health.request_timeouts == 0
                assert protocol.health.request_timeouts == 0
            assert not protocol._pending
            assert protocol.active and not transport.closed
            assert api._subscription_endpoints == [endpoint]
            assert endpoint.registration_id == registration_id
            assert [
                len(socket.sent) - before
                for socket, before in zip(sockets, sent_before)
            ] == [1, 1]

            # A stable ID cannot distinguish replies from two overlapping
            # cycles; test late arrival while no new ping is outstanding.
            last_seen = api.health.last_seen
            acknowledge(protocol, api, endpoint.keep_alive_id)
            await tick()
            assert protocol._discarded_packets["unmatched_reply"] == 1
            assert api.health.last_seen is last_seen
            assert api.on_subscription_report.await_count == 3

            # Resume on the existing socket, without replaying the missed ping
            # or recreating subscriptions after either timeout or cancellation.
            transport.reply = True
            protocol._response_timeout = 5
            assert await api.ping()
            assert api._subscription_endpoints == [endpoint]
            assert len(sockets) == 2 and not transport.closed
            assert endpoint.registration_id == registration_id

        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())
