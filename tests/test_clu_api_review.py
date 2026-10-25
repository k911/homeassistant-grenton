"""Protocol regressions for reconnect races, report validation and batching."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from test_clu_lifecycle import fixture
from test_clu_protocol_lifecycle import socket_factory, tick

from custom_components.homeassistant_grenton.domain.api.clu import GrentonCluApiProtocol
from custom_components.homeassistant_grenton.domain.api.clu_messages import (
    GrentonCluApiPingRequest,
)
from custom_components.homeassistant_grenton.state import (
    MISSING_STATE,
    GrentonCluStateVariableKey,
)


def test_disconnect_detaches_old_sockets_before_waiting_for_callbacks(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        with socket_factory(api, sockets):
            await api.connect()
            await api.register_component_states([GrentonCluStateVariableKey("a")])
            old_main, subscription = sockets
            started, release = asyncio.Event(), asyncio.Event()

            async def report(values):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    await release.wait()

            subscription.protocol.subscription_callback = report
            subscription.protocol._process_response(
                "resp:192.0.2.1:00000000:clientReport:0:{1}"
            )
            await started.wait()
            shutdown = asyncio.create_task(api.disconnect())
            await tick()
            try:
                assert await api.connect()
                new_main = api.transport
                assert new_main is not old_main
            finally:
                release.set()
                await shutdown
            assert api.transport is new_main and not new_main.closed
            assert await api.ping()
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_old_registration_cannot_open_more_sockets_after_reconnect(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        started, release = asyncio.Event(), asyncio.Event()
        with socket_factory(api, sockets):
            original_create = asyncio.get_running_loop().create_datagram_endpoint

            async def create(factory, **kwargs):
                result = await original_create(factory, **kwargs)
                if len(sockets) == 1:
                    started.set()
                    await release.wait()
                return result

            with patch.object(
                asyncio.get_running_loop(),
                "create_datagram_endpoint",
                side_effect=create,
            ):
                keys = [GrentonCluStateVariableKey(str(i)) for i in range(31)]
                registration = asyncio.create_task(api.register_component_states(keys))
                await started.wait()
                await api.disconnect()
                assert await api.connect()
                release.set()
                assert await registration is None
                assert len(sockets) == 2
                assert not api._subscription_endpoints
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_poll_discards_a_reply_from_before_disconnect_and_reconnect(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        with socket_factory(api, sockets):
            await api.connect()
            started, release = asyncio.Event(), asyncio.Event()

            async def read(request):
                started.set()
                await release.wait()
                return f"resp:192.0.2.1:{request.msg_id}:clientReport:1:{{99}}"

            with patch.object(api.protocol, "send_request", side_effect=read):
                task = asyncio.create_task(
                    api.poll_component_states([GrentonCluStateVariableKey("a")])
                )
                await started.wait()
                await api.disconnect()
                await api.connect()
                release.set()
                assert await task == [MISSING_STATE]
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_reordered_and_duplicate_keys_reuse_existing_subscription_groups(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        keys = [GrentonCluStateVariableKey(str(i)) for i in range(31)]
        with socket_factory(api, sockets):
            await api.connect()
            assert await api.register_component_states(keys) == [1] * 31
            endpoints = api._subscription_endpoints.copy()
            assert (
                await api.register_component_states([*reversed(keys), keys[0]])
                == [1] * 32
            )
            assert api._subscription_endpoints == endpoints
            assert len(sockets) == 3
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload", ["clientReport:bad:{}", "clientReport:0:bad", "clientReport:-1:{}"]
)
def test_malformed_empty_reports_do_not_refresh_health_or_reach_consumer(
    tmp_path, payload
):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        protocol = GrentonCluApiProtocol(api)
        protocol.subscription_callback = AsyncMock()
        protocol._process_response(f"resp:192.0.2.1:00000000:{payload}")
        await tick()
        protocol.subscription_callback.assert_not_awaited()
        assert api.health.last_seen is None
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_non_response_with_matching_id_cannot_finish_request(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        with socket_factory(api, [], reply=False):
            await api.connect()
            task = asyncio.create_task(
                api.protocol.send_request(GrentonCluApiPingRequest("12345678"))
            )
            await tick()
            api.protocol._process_response("req:192.0.2.1:12345678:fake")
            await tick()
            assert not task.done() and api.health.last_seen is None
            api.protocol._process_response("resp:192.0.2.1:12345678:pong")
            assert (await task).endswith(":pong")
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_cancelled_disconnect_still_closes_every_transport(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def report(values):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelling.set()
                await release.wait()

        with socket_factory(api, sockets):
            await api.connect()
            await api.register_component_states(
                [GrentonCluStateVariableKey(str(i)) for i in range(31)]
            )
            sockets[1].protocol.subscription_callback = report
            sockets[1].protocol._process_response(
                "resp:192.0.2.1:00000000:clientReport:0:{" + ",".join(["1"] * 30) + "}"
            )
            await started.wait()
            shutdown = asyncio.create_task(api.disconnect())
            await cancelling.wait()
            assert all(socket.closed for socket in sockets)
            shutdown.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await shutdown
            assert not api._subscription_endpoints
            assert api.transport is api.protocol is None
            await tick()
            assert all(not socket.protocol._report_tasks for socket in sockets)
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_report_consumer_can_initiate_disconnect_without_deadlocking(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets, finished = [], asyncio.Event()

        async def report(keys, values):
            await api.disconnect()
            finished.set()

        with socket_factory(api, sockets):
            await api.connect()
            await api.register_component_states([GrentonCluStateVariableKey("a")])
            api.on_subscription_report = report
            sockets[1].protocol._process_response(
                "resp:192.0.2.1:00000000:clientReport:0:{1}"
            )
            await asyncio.wait_for(finished.wait(), 0.5)
            assert all(socket.closed for socket in sockets)
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_initial_keepalive_connection_loss_does_not_abort_healthy_groups(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        keys = [GrentonCluStateVariableKey(str(i)) for i in range(31)]
        sockets = []
        with socket_factory(api, sockets):
            await api.connect()
            send = GrentonCluApiProtocol.send_request
            first_subscription = True

            async def send_with_failure(protocol, request):
                nonlocal first_subscription
                if protocol.subscription_keys is not None and first_subscription:
                    first_subscription = False
                    raise ConnectionError("Lost initial subscription socket")
                return await send(protocol, request)

            with patch.object(GrentonCluApiProtocol, "send_request", send_with_failure):
                assert await api.register_component_states(keys) == [
                    MISSING_STATE
                ] * 30 + [1]
            first, healthy = api._subscription_endpoints
            assert first.protocol is None and healthy.protocol is not None
            assert await api.register_component_states(keys) == [1] * 31
            assert api._subscription_endpoints[1] is healthy
            assert len(sockets) == 4
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_changing_large_value_policy_rebuilds_with_the_value_isolated(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        keys = [GrentonCluStateVariableKey("a"), GrentonCluStateVariableKey("schedule")]
        sockets = []
        with socket_factory(api, sockets):
            await api.connect()
            assert await api.register_component_states(keys) == [1, 1]
            api.large_value_keys.add(keys[1])
            assert await api.register_component_states(keys) == [1, 1]
            assert [endpoint.keys for endpoint in api._subscription_endpoints] == [
                [keys[0]],
                [keys[1]],
            ]
            assert sockets[1].closed
            api.protocol.send_request = AsyncMock(
                return_value="resp:192.0.2.1:abcd:clientReport:1:{7}"
            )
            assert await api.poll_component_states([keys[0], keys[0]]) == [7, 7]
            api.protocol.send_request.assert_awaited_once()
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("report_session", [0, 1, 7])
@pytest.mark.parametrize("poll", [False, True])
def test_matched_read_reply_does_not_require_session_echo(
    tmp_path, caplog, report_session, poll
):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        key = GrentonCluStateVariableKey("temperature")
        process = GrentonCluApiProtocol._process_response

        def incoming(protocol, wire):
            if ":clientReport:" in wire:
                parts = wire.split(":", 5)
                parts[4] = str(report_session)
                wire = ":".join(parts)
            process(protocol, wire)

        with (
            socket_factory(api, sockets),
            patch.object(GrentonCluApiProtocol, "_process_response", incoming),
        ):
            await api.connect()
            read = api.poll_component_states if poll else api.register_component_states
            assert await read([key]) == [1]
            assert await read([key]) == [1]
            assert len(sockets) == (1 if poll else 2)
            requests = [
                raw
                for socket in sockets
                for raw, _ in socket.sent
                if "clientRegister" in raw
            ]
            assert len(requests) == 2
            # Request semantics stay unchanged, even when the reply differs.
            assert all(
                f"clientRegister(0,{1 if poll else 0},1," in raw for raw in requests
            )
            assert "Failed to parse register response" not in caplog.text
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_non_echoing_session_does_not_bypass_peer_socket_or_message_correlation(
    tmp_path,
):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        with socket_factory(api, sockets, reply=False):
            await api.connect()
            protocol = api.protocol
            read = asyncio.create_task(
                api.poll_component_states([GrentonCluStateVariableKey("a")])
            )
            await tick()
            message_id = next(iter(protocol._pending))
            wire = f"resp:{api.clu.ip}:{message_id}:clientReport:7:{{42}}".encode()
            peer = (api.clu.ip, api.clu.port)
            protocol.datagram_received(wire, ("192.0.2.99", api.clu.port))
            protocol.datagram_received(wire, (api.clu.ip, api.clu.port + 1))
            protocol.datagram_received(
                f"resp:{api.clu.ip}:unmatched:clientReport:7:{{99}}".encode(), peer
            )
            protocol.datagram_received(
                f"resp:{api.clu.ip}:00000000:clientReport:7:{{99}}".encode(), peer
            )
            other_socket = GrentonCluApiProtocol(api)
            other_socket.datagram_received(wire, peer)
            await tick()
            assert not read.done() and message_id in protocol._pending
            protocol.datagram_received(wire, peer)
            assert await read == [42]
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())
