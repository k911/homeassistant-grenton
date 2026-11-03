"""The live probe verifies wire cleanup, rather than only silencing callbacks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import shared_subscription_probe as probe


class ProbeApi:
    def __init__(self):
        self.clu = SimpleNamespace(name="Main", object_name="CLU1")
        self.subscription_id = 2000
        self.large_value_keys = set()
        self._groups = []
        self.transport = object()
        self.report_transport = object()
        self.protocol = SimpleNamespace(active=True, send_request=self.send)
        self.report_protocol = SimpleNamespace(active=True, _received_report_datagrams=0)
        self.disconnects = 0

    async def send(self, request):
        payload = str(request.subscription_id) if isinstance(request, probe.GrentonCluApiClientDestroyRequest) else "ok"
        return f"resp:192.0.2.1:{request.msg_id}:{payload}"

    async def connect(self):
        return True

    async def ping(self):
        return await self.protocol.send_request(probe.GrentonCluApiPingRequest()) is not None

    async def async_set_subscription_id(self, value):
        self.subscription_id = value

    async def register_component_states(self, keys):
        if self._groups and (not keys or self._groups[0].subscription_id != self.subscription_id):
            for group in self._groups:
                await self.protocol.send_request(probe.GrentonCluApiClientDestroyRequest(group.subscription_id))
            self._groups = []
        if not self._groups:
            self._groups = [
                SimpleNamespace(keys=[key], subscription_id=self.subscription_id + index)
                for index, key in enumerate(keys)
            ]
        for group in self._groups:
            await self.protocol.send_request(
                probe.GrentonCluApiClientRegisterRequest(group.keys, 0, subscription_id=group.subscription_id)
            )
        return [100, 200] if keys else []

    @property
    def subscription_count(self):
        return len(self._groups)

    async def fetch_component_states(self, keys):
        return [300]

    async def disconnect(self):
        self.disconnects += 1
        self.transport = self.report_transport = None


async def run_probe(*, continued_remote_reports=False, cancel=False):
    api = ProbeApi()
    phase = 0

    async def periodic(interval, operation):
        for _ in range(2 if interval == 45 else 1):
            await operation()
        await asyncio.Event().wait()

    async def observe(seconds):
        nonlocal phase
        await asyncio.sleep(0)
        if cancel:
            raise asyncio.CancelledError
        if phase < 2:
            for offset in range(3):
                for group in api._groups:
                    api.report_protocol._received_report_datagrams += 1
                    await api.on_subscription_report(group.keys, [100 + phase * 10 + offset])
        elif continued_remote_reports:
            # The callback is absent because the group was retired, but actual
            # network traffic continuing must still make the probe fail.
            api.report_protocol._received_report_datagrams += 1
        phase += 1

    with (
        patch.object(probe, "_periodic", periodic),
        patch.object(probe, "_observe", observe),
    ):
        result = await probe.verify_shared_subscriptions(api)
    return api, result


def test_shared_probe_confirms_routing_rebase_and_acknowledged_wire_cleanup():
    async def run():
        api, result = await run_probe()
        assert result["result"] == "passed"
        assert result["initial_ids"] == [2000, 2001]
        assert result["rebased_ids"] == [2010, 2011]
        assert result["reports_after_cleanup"] == 0
        assert result["traffic"]["destroy_acknowledgements"] == 4
        assert result["local_sockets_closed"] is True
        assert api.disconnects == 1

    asyncio.run(run())


def test_shared_probe_does_not_confuse_retired_callback_with_remote_cleanup():
    async def run():
        api, result = await run_probe(continued_remote_reports=True)
        assert result["groups_after_cleanup"] == 0
        assert result["reports_after_cleanup"] == 1
        assert result["result"] == "failed"
        assert api.disconnects == 1

    asyncio.run(run())


def test_cancelled_probe_still_disconnects_and_propagates_cancellation():
    async def run():
        api = ProbeApi()

        async def cancelled(seconds):
            raise asyncio.CancelledError

        with patch.object(probe, "_observe", cancelled), pytest.raises(asyncio.CancelledError):
            await probe.verify_shared_subscriptions(api)
        assert api.disconnects == 1
        assert api.transport is api.report_transport is None

    asyncio.run(run())
