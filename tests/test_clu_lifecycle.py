"""Bounded subscription work and socket ownership across Home Assistant stops."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError
from test_clu_scripts import make_entry, make_hass
from test_clu_state import coordinator, make_clu

from custom_components.homeassistant_grenton import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.homeassistant_grenton.domain.api.clu import (
    GrentonCluApiProtocol,
    _SubscriptionEndpoint,
)
from custom_components.homeassistant_grenton.domain.api.clu_messages import (
    GrentonCluApiPingRequest,
)
from custom_components.homeassistant_grenton.domain.state_object import (
    GrentonVariableValueObject,
)
from custom_components.homeassistant_grenton.state import (
    MISSING_STATE,
    GrentonCluStateVariableKey,
)


async def fixture(tmp_path):
    entry = make_entry()
    hass = await make_hass(tmp_path, [entry])
    coord = coordinator(hass, entry, [make_clu("clu1")])
    return hass, entry, coord, coord._apis["clu1"]


def synthetic_subscription(api, values):
    stats = {"active": 0, "peak": 0}

    async def open_socket(keys):
        async def read(request):
            stats["active"] += 1
            stats["peak"] = max(stats["peak"], stats["active"])
            await asyncio.sleep(0)
            stats["active"] -= 1
            payload = ",".join(json.dumps(values[key]) for key in request.keys)
            return f"resp:192.0.2.1:{request.msg_id}:clientReport:0:{{{payload}}}"

        api._subscription_endpoints.append(
            _SubscriptionEndpoint(
                Mock(),
                SimpleNamespace(send_request=AsyncMock(side_effect=read)),
                keys,
                0,
            )
        )

    api._open_chunk_socket = AsyncMock(side_effect=open_socket)
    return stats


def test_pack_ordinary_values_across_schedules_and_only_refresh_required_groups(
    tmp_path,
):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        keys = [GrentonCluStateVariableKey(str(i)) for i in range(61)]
        large = {keys[29], keys[60]}
        api.large_value_keys = large
        values = {
            key: "Y0" * 672 + "+00:30" if key in large else index
            for index, key in enumerate(keys)
        }
        stats = synthetic_subscription(api, values)
        assert await api.register_component_states(keys) == list(values.values())
        assert stats["peak"] == 1
        endpoints = api._subscription_endpoints.copy()
        ids = [endpoint.registration_id for endpoint in endpoints]
        assert [len(endpoint.keys) for endpoint in endpoints] == [30, 29, 1, 1]
        assert api._open_chunk_socket.await_count == 4
        for endpoint in endpoints:
            endpoint.protocol.send_request.reset_mock()
        selected = {keys[0], keys[29]}
        result = await api.register_component_states(keys, required_keys=selected)
        assert [
            endpoint.protocol.send_request.await_count for endpoint in endpoints
        ] == [1, 0, 1, 0]
        assert result[29] == values[keys[29]]
        assert result[30] == values[keys[30]]  # Same ordinary group is returned whole.
        assert result[31] is MISSING_STATE
        assert result[60] is MISSING_STATE
        assert [
            endpoint.registration_id for endpoint in api._subscription_endpoints
        ] == ids
        assert api._open_chunk_socket.await_count == 4
        assert await api.register_component_states(keys) == list(values.values())
        await coord.async_shutdown()
        for endpoint in endpoints:
            endpoint.transport.close.assert_called_once()
        await hass.async_stop()

    asyncio.run(run())


def test_coordinator_targeted_read_preserves_unrelated_cached_state(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        states = [GrentonVariableValueObject("clu1", "", str(i)) for i in range(61)]
        for state in states:
            coord.register_component_state(state)
        keys = coord.state.clus["clu1"].get_subscription_order()
        values = dict(zip(keys, range(61)))
        synthetic_subscription(api, values)
        assert await coord.async_refresh_clu_state("clu1")
        endpoints = api._subscription_endpoints.copy()
        for endpoint in endpoints:
            endpoint.protocol.send_request.reset_mock()
        values[keys[0]] = 999
        values[keys[31]] = 888
        assert await coord.async_refresh_clu_state("clu1", required_states=[states[0]])
        assert coord.get_value_for_component(states[0]) == 999
        assert coord.get_value_for_component(states[31]) == 31
        assert [
            endpoint.protocol.send_request.await_count for endpoint in endpoints
        ] == [1, 0, 0]
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_ha_stop_cancels_loops_closes_all_sockets_and_is_idempotent(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        main = Mock()
        sockets = [Mock(), Mock()]
        api.connect = AsyncMock(return_value=True)
        api.ping = AsyncMock(return_value=True)
        api.transport = main
        api.protocol = Mock()
        api._subscription_endpoints = [
            _SubscriptionEndpoint(sock, Mock(), [GrentonCluStateVariableKey(str(i))], 0)
            for i, sock in enumerate(sockets)
        ]
        await coord.async_setup()
        tasks = [coord._ping_task, coord._register_task]
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert all(task.done() for task in tasks)
        assert coord._apis == {}
        assert api._subscription_endpoints == []
        assert api.transport is None and api.protocol is None
        assert api._closed and api.on_subscription_report is None
        for sock in [main, *sockets]:
            sock.close.assert_called_once()
        await coord.async_shutdown()
        for sock in [main, *sockets]:
            sock.close.assert_called_once()
        assert (
            await api.register_component_states(
                [GrentonCluStateVariableKey("after_stop")]
            )
            is None
        )
        await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_shutdown_or_cancellation_during_subscription_ping_cannot_leak_socket(
    tmp_path, cancel
):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        started = asyncio.Event()
        pending = asyncio.Future()
        transport = Mock()

        async def create(factory, **kwargs):
            return transport, factory()

        async def initial_ping(request):
            started.set()
            return await pending

        with (
            patch.object(
                asyncio.get_running_loop(),
                "create_datagram_endpoint",
                side_effect=create,
            ),
            patch.object(
                GrentonCluApiProtocol, "send_request", side_effect=initial_ping
            ),
        ):
            task = asyncio.create_task(
                api._open_chunk_socket([GrentonCluStateVariableKey("a")])
            )
            await started.wait()
            assert len(api._subscription_endpoints) == 1
            assert not api._subscription_endpoints[0].ready
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await api.disconnect()
                pending.set_result(None)
                await task
            assert api._subscription_endpoints == []
            transport.close.assert_called_once()
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_socket_created_after_disconnect_is_immediately_closed(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        transport = Mock()
        started, release = asyncio.Event(), asyncio.Event()

        async def create(factory, **kwargs):
            started.set()
            await release.wait()
            return transport, factory()

        with patch.object(
            asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=create
        ):
            task = asyncio.create_task(api.connect())
            await started.wait()
            await api.disconnect()
            release.set()
            assert not await task
        transport.close.assert_called_once()
        assert api.transport is None and api.protocol is None
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_closing_protocol_fails_pending_requests_without_waiting_for_timeout(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        protocol = GrentonCluApiProtocol(api)
        api.cipher = SimpleNamespace(encrypt=lambda value: value)
        sent = asyncio.Event()
        protocol.transport = SimpleNamespace(sendto=lambda *args: sent.set())
        task = asyncio.create_task(
            protocol.send_request(GrentonCluApiPingRequest("00000001"))
        )
        await sent.wait()
        protocol.connection_lost(None)
        with pytest.raises(ConnectionError):
            await task
        assert protocol.transport is None and protocol._pending == {}
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("unloaded", [False, True])
def test_unload_failure_keeps_the_existing_connection_alive(tmp_path, unloaded):
    async def run():
        hass, entry, coord, _ = await fixture(tmp_path)
        entry.runtime_data = SimpleNamespace(coordinator=coord)
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=unloaded)
        coord.async_shutdown = AsyncMock()
        assert await async_unload_entry(hass, entry) is unloaded
        assert coord.async_shutdown.await_count == int(unloaded)
        await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["coordinator", "platform"])
def test_setup_failure_closes_the_coordinator(tmp_path, stage):
    async def run():
        hass, entry, coord, _ = await fixture(tmp_path)
        interface = {
            "id": "test",
            "version": 1,
            "name": "Home",
            "icon": "home",
            "theme": "GRENTON",
            "encryption": {"key": "00" * 16, "iv": "00" * 16},
            "clus": [],
            "pages": [],
            "pushNotifications": [],
        }
        entry = make_entry(interface=interface)
        coord.config_entry = entry
        coord.async_setup = AsyncMock(
            side_effect=HomeAssistantError("setup failed")
            if stage == "coordinator"
            else None
        )
        coord.async_shutdown = AsyncMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock(
            side_effect=HomeAssistantError("setup failed")
            if stage == "platform"
            else None
        )
        with (
            patch(
                "custom_components.homeassistant_grenton.GrentonCoordinator",
                return_value=coord,
            ),
            pytest.raises(HomeAssistantError, match="setup failed"),
        ):
            await async_setup_entry(hass, entry)
        coord.async_shutdown.assert_awaited_once()
        await hass.async_stop()

    asyncio.run(run())


def test_startup_retries_only_failed_subscription_groups(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        states = [GrentonVariableValueObject("clu1", "", str(i)) for i in range(61)]
        for state in states:
            coord.register_component_state(state)
        keys = coord.state.clus["clu1"].get_subscription_order()
        values = dict(zip(keys, range(61)))
        synthetic_subscription(api, values)
        original = api._open_chunk_socket.side_effect

        async def open_socket(chunk):
            await original(chunk)
            if keys[31] in chunk:
                endpoint = api._subscription_endpoints[-1]
                read = endpoint.protocol.send_request.side_effect
                failed = False

                async def fail_once(request):
                    nonlocal failed
                    if not failed:
                        failed = True
                        return None
                    return await read(request)

                endpoint.protocol.send_request.side_effect = fail_once

        api._open_chunk_socket.side_effect = open_socket
        api.connect = AsyncMock(return_value=True)
        api.ping = AsyncMock(return_value=True)
        await coord.async_setup()
        assert [
            endpoint.protocol.send_request.await_count
            for endpoint in api._subscription_endpoints
        ] == [1, 2, 1]
        assert coord.get_value_for_component(states[31]) == 31
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_cli_can_reconnect_without_losing_its_single_socket_keepalive(tmp_path):
    from custom_components.homeassistant_grenton.domain.api.clu_cli import (
        GrentonCluCliApi,
    )

    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        cli = GrentonCluCliApi(api.clu, api.encryption)
        transports = []

        async def create(factory, **kwargs):
            transport = Mock()
            transports.append(transport)
            return transport, factory()

        with (
            patch.object(
                asyncio.get_running_loop(),
                "create_datagram_endpoint",
                side_effect=create,
            ),
            patch.object(
                GrentonCluApiProtocol,
                "send_request",
                new_callable=AsyncMock,
                return_value="resp:192.0.2.1:00000001:pong",
            ),
        ):
            for _ in range(2):
                assert await cli.connect()
                assert (
                    await cli.connect()
                )  # Repeated connect does not open another socket.
                assert await cli.ping()
                assert cli._subscription_endpoints == []
                await cli.disconnect()
        assert len(transports) == 2
        for transport in transports:
            transport.close.assert_called_once()
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())
