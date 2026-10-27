"""Captured fetchValues reads do not create or disturb realtime subscriptions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from test_clu_lifecycle import fixture
from test_clu_protocol_lifecycle import socket_factory, tick
from test_clu_state import make_clu

from custom_components.homeassistant_grenton.domain.api.clu_messages import (
    GrentonCluApiFetchValuesRequest,
    GrentonCluApiFetchValuesResponse,
)
from custom_components.homeassistant_grenton.state import (
    MISSING_STATE,
    GrentonCluStateAttributeKey,
    GrentonCluStateVariableKey,
)


def test_fetch_request_matches_captured_thermostat_payload():
    keys = [
        GrentonCluStateAttributeKey("THE6183", index) for index in ("10", "11", "9")
    ]
    request = GrentonCluApiFetchValuesRequest(keys, "1234abcd")
    assert request.raw == (
        "req::1234abcd:SYSTEM:fetchValues({{THE6183,10},{THE6183,11},{THE6183,9}})"
    )
    mixed = GrentonCluApiFetchValuesRequest(
        [GrentonCluStateVariableKey('room"name'), keys[0]]
    )
    assert mixed.payload == 'SYSTEM:fetchValues({"room\\"name",{THE6183,10}})'
    with pytest.raises(ValueError):
        GrentonCluApiFetchValuesRequest([])


@pytest.mark.parametrize(
    "content,expected",
    [
        ('22.5,true,false,nil,"room, lamp"', [22.5, True, False, None, "room, lamp"]),
        ('1,"room "left,right" label",0', [1, 'room "left,right" label', 0]),
        ('"  Y0,Y0+00:30  ",23', ["  Y0,Y0+00:30  ", 23]),
    ],
)
def test_fetch_response_uses_shared_coercion_and_count_aware_strings(content, expected):
    keys = [GrentonCluStateVariableKey(str(index)) for index in range(len(expected))]
    response = GrentonCluApiFetchValuesResponse(
        f"resp:192.0.2.1:1234abcd:values:{{{content}}}", keys
    )
    assert response.values == expected


def test_fetch_response_preserves_firmware_identifiers():
    clu = make_clu("clu1")
    keys = [
        GrentonCluStateAttributeKey(clu.object_name, "17"),
        GrentonCluStateVariableKey("number"),
    ]
    response = GrentonCluApiFetchValuesResponse(
        'resp:192.0.2.1:1234abcd:values:{"1.20","00123"}', keys, clu
    )
    assert response.values == ["1.20", 123]


@pytest.mark.parametrize(
    "payload",
    ["clientReport:1:{1,2}", "values:1,2", "values:{1}", "values:{1,2,3}"],
)
def test_fetch_response_rejects_wrong_prefix_structure_and_nonempty_count(payload):
    keys = [GrentonCluStateVariableKey("a"), GrentonCluStateVariableKey("b")]
    with pytest.raises(ValueError):
        GrentonCluApiFetchValuesResponse(f"resp:192.0.2.1:1234abcd:{payload}", keys)


def test_invalid_fetch_envelope_does_not_log_returned_values(tmp_path, caplog):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        api.protocol = SimpleNamespace(
            send_request=AsyncMock(return_value='resp:::values:{"private-state"}')
        )
        key = GrentonCluStateVariableKey("value")
        assert await api.fetch_component_states([key]) == [MISSING_STATE]
        assert "Invalid fetchValues response envelope" in caplog.text
        assert "private-state" not in caplog.text
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_fetch_batches_deduplicate_keys_and_isolate_large_values(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        keys = [GrentonCluStateVariableKey(str(index)) for index in range(65)]
        api.large_value_keys = {keys[29], keys[60]}
        week = "  " + "Y0" * 672 + "+00:30  "
        values = {
            key: week if key in api.large_value_keys else index
            for index, key in enumerate(keys)
        }
        requests = []

        async def read(request):
            requests.append(request)
            content = ",".join(
                f'"{values[key]}"' if isinstance(values[key], str) else str(values[key])
                for key in request.keys
            )
            return f"resp:192.0.2.1:{request.msg_id}:values:{{{content}}}"

        api.protocol = SimpleNamespace(send_request=AsyncMock(side_effect=read))
        api._ensure_report_listener = AsyncMock()
        assert await api.fetch_component_states([*keys, keys[0]]) == [
            *values.values(),
            values[keys[0]],
        ]
        assert [len(request.keys) for request in requests] == [30, 30, 3, 1, 1]
        assert all(
            isinstance(request, GrentonCluApiFetchValuesRequest) for request in requests
        )
        assert len({request.msg_id for request in requests}) == 5
        assert await api.fetch_component_states([]) == []
        assert len(requests) == 5
        api._ensure_report_listener.assert_not_awaited()
        assert not api._groups
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_fetch_failed_batch_keeps_alignment_and_distinguishes_nil(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        keys = [GrentonCluStateVariableKey(str(index)) for index in range(31)]
        api.protocol = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    None,
                    "resp:192.0.2.1:1234abcd:values:{nil}",
                    "resp:192.0.2.1:1234abcd:values:{1,2}",
                    "resp:192.0.2.1:1234abcd:values:{}",
                    ConnectionError("closed"),
                ]
            )
        )
        assert await api.fetch_component_states(keys) == [MISSING_STATE] * 30 + [None]
        for _ in range(3):
            assert await api.fetch_component_states(keys[:1]) == [MISSING_STATE]
        await coord.async_shutdown()
        assert await api.fetch_component_states(keys[:1]) == [MISSING_STATE]
        await hass.async_stop()

    asyncio.run(run())


def test_fetch_correlates_encrypted_reply_on_existing_control_socket(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        cipher, sockets, sent = api.cipher, [], []
        with socket_factory(api, sockets, reply=False):
            await api.connect()
            api.cipher = cipher
            protocol = api.protocol

            def send(encrypted, peer):
                request = cipher.decrypt(encrypted).decode()
                sent.append(request)
                message_id = request.split(":", 3)[2]
                other = cipher.encrypt(
                    f"resp:{api.clu.ip}:unmatched:values:{{99}}".encode()
                )
                reply = cipher.encrypt(
                    f"resp:{api.clu.ip}:{message_id}:values:{{22.5}}".encode()
                )
                loop = asyncio.get_running_loop()
                loop.call_soon(protocol.datagram_received, other, peer)
                loop.call_soon(
                    protocol.datagram_received, reply, ("192.0.2.99", peer[1])
                )
                loop.call_soon(protocol.datagram_received, reply, peer)

            sockets[0].sendto = send
            key = GrentonCluStateAttributeKey("THE6183", "14")
            assert await api.fetch_component_states([key]) == [22.5]
            assert len(sent) == 1 and sent[0].endswith(
                ":SYSTEM:fetchValues({{THE6183,14}})"
            )
            assert len(sockets) == 1 and not api._groups
            assert not protocol._pending
            assert protocol._discarded_packets["wrong_peer"] == 1
            assert protocol._discarded_packets["unmatched_reply"] == 1
            await coord.async_shutdown()
            assert sockets[0].closed and protocol.closed
        await hass.async_stop()

    asyncio.run(run())


def test_fetch_preserves_shared_report_listener_and_group_ids(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        with socket_factory(api, sockets):
            await api.connect()
            key = GrentonCluStateVariableKey("value")
            await api.register_component_states([key])
            group = api._groups[0]
            identity = (
                api.report_transport,
                api.report_protocol,
                group.subscription_id,
            )
            with patch.object(
                api.protocol,
                "send_request",
                return_value="resp:192.0.2.1:1234abcd:values:{42}",
            ):
                assert await api.fetch_component_states([key]) == [42]
            assert api._groups == [group]
            assert (
                api.report_transport,
                api.report_protocol,
                group.subscription_id,
            ) == identity
            assert len(sockets) == 2 and not api.report_transport.closed
            await coord.async_shutdown()
            assert all(socket.closed for socket in sockets)
        await hass.async_stop()

    asyncio.run(run())


def test_fetch_cancellation_removes_waiter_without_opening_more_sockets(tmp_path):
    async def run():
        hass, _, coord, api = await fixture(tmp_path)
        sockets = []
        with socket_factory(api, sockets, reply=False):
            await api.connect()
            protocol = api.protocol
            task = asyncio.create_task(
                api.fetch_component_states([GrentonCluStateVariableKey("a")])
            )
            for _ in range(10):
                await tick()
                if protocol._pending:
                    break
            assert protocol._pending
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not protocol._pending and not api._groups
            assert len(sockets) == 1
            await coord.async_shutdown()
            assert sockets[0].closed
        await hass.async_stop()

    asyncio.run(run())
