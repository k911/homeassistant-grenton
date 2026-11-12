"""The temporary subscription probe proves report cessation without state writes."""

import asyncio
import contextlib
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import subscription_probe as probe
from commands import create_parser
from homeassistant_grenton.state import MISSING_STATE

SUBSCRIPTION_ID = 3456
REAL_SLEEP = asyncio.sleep


class ProbeHarness:
    """Model just one control socket, with deterministic report timestamps."""

    def __init__(self):
        self.requests = []
        self.baseline = [(7, [107]), (8, [108]), (9, [109])]
        self.grace = []
        self.after = []
        self.clock = 0
        self.sleep_phase = 0
        self.keepalive_ticks = asyncio.Queue()
        self.keepalive_task = None
        self.cancel_baseline = False
        self.registration_payload = f"clientReport:{SUBSCRIPTION_ID}:{{100}}"
        self.destroy_payloads = [str(SUBSCRIPTION_ID)]
        self.protocol = SimpleNamespace(
            send_request=AsyncMock(side_effect=self.send),
            transport=object(),
            active=True,
        )
        self.api = SimpleNamespace(
            clu=SimpleNamespace(
                id="CLU1", object_name="CLU1", name="Main", ip="192.0.2.1",
                device_type="CLU",
            ),
            protocol=self.protocol,
            connect=AsyncMock(return_value=True),
            ping=AsyncMock(return_value=True),
            fetch_component_states=AsyncMock(return_value=[121]),
            disconnect=AsyncMock(),
        )

    async def send(self, request):
        self.requests.append(request)
        if isinstance(request, probe.GrentonCluApiClientRegisterRequest):
            payload = self.registration_payload
        elif isinstance(request, probe.GrentonCluApiClientDestroyRequest):
            payload = self.destroy_payloads.pop(0)
        else:
            raise TypeError(f"Unexpected request type: {type(request).__name__}")
        if payload is None:
            return None
        return f"resp:192.0.2.1:{request.msg_id}:{payload}"

    async def sleep(self, seconds):
        if seconds == 0:
            await REAL_SLEEP(0)
            return
        if asyncio.current_task() is not self.probe_task:
            self.keepalive_task = asyncio.current_task()
            await self.keepalive_ticks.get()
            return
        phase = self.sleep_phase
        self.sleep_phase += 1
        # Let the background keepalive start and block on its own timer.
        await REAL_SLEEP(0)
        if phase == 0 and self.cancel_baseline:
            raise asyncio.CancelledError
        start, end = self.clock, self.clock + seconds
        events = [
            (start + offset, values)
            for offset, values in (self.baseline, self.grace, self.after)[phase]
        ]
        events.extend(
            (tick, None)
            for tick in range(10, 30, 10)
            if start < tick <= end
        )
        for timestamp, values in sorted(events):
            self.clock = timestamp
            if values is None:
                self.keepalive_ticks.put_nowait(True)
                await REAL_SLEEP(0)
            else:
                await self.protocol.subscription_callback(values)
        self.clock = end

    async def run(self):
        self.probe_task = asyncio.current_task()
        with (
            patch.object(probe.secrets, "randbelow", return_value=SUBSCRIPTION_ID - 1024),
            patch.object(probe, "monotonic", side_effect=lambda: self.clock),
            patch.object(probe.asyncio, "sleep", side_effect=self.sleep),
        ):
            return await probe.verify_client_destroy(self.api, 10)

    def assert_closed(self):
        self.api.disconnect.assert_awaited_once()
        if self.keepalive_task is not None:
            assert self.keepalive_task.done()


def test_changing_baseline_then_no_reports_verifies_destruction_on_same_socket():
    async def run():
        harness = ProbeHarness()
        result = await harness.run()
        assert result["result"] == "passed"
        assert result["reports_before"] == 3
        assert result["reports_after"] == 0
        assert result["destroy_acknowledged"] is True
        assert result["baseline_active"] is True
        assert result["sufficient_observation_window"] is True
        assert result["same_socket"] is True
        assert [request.payload for request in harness.requests] == [
            "SYSTEM:clientRegister(0,0,3456,{{CLU1,0}})",
            "SYSTEM:clientDestroy(0,0,3456)",
        ]
        assert [key.name for key in harness.protocol.subscription_keys] == ["0"]
        assert len({request.msg_id for request in harness.requests}) == 2
        harness.api.ping.assert_awaited()
        assert harness.api.ping.await_count == 4
        assert result["keepalive_failures"] == 0
        harness.api.fetch_component_states.assert_awaited_once_with(
            harness.protocol.subscription_keys
        )
        harness.assert_closed()

    asyncio.run(run())


def test_continuing_reports_fail_even_when_destroy_is_acknowledged():
    async def run():
        harness = ProbeHarness()
        harness.after = [(1, [112])]
        result = await harness.run()
        assert result["destroy_acknowledged"] is True
        assert result["reports_after"] == 1
        assert result["result"] == "failed"
        harness.assert_closed()

    asyncio.run(run())


def test_queued_report_during_grace_is_separate_from_verification_window():
    async def run():
        harness = ProbeHarness()
        harness.grace = [(0.1, [110])]
        result = await harness.run()
        assert result["reports_during_grace"] == 1
        assert result["reports_after"] == 0
        assert result["result"] == "passed"
        harness.assert_closed()

    asyncio.run(run())


def test_report_delivered_during_final_responsiveness_check_still_fails():
    async def run():
        harness = ProbeHarness()

        async def final_read(keys):
            await harness.protocol.subscription_callback([121])
            return [121]

        harness.api.fetch_component_states.side_effect = final_read
        result = await harness.run()
        assert result["reports_after"] == 1
        assert result["result"] == "failed"
        harness.assert_closed()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["protocol", "transport", "inactive"])
def test_socket_replacement_or_closure_cannot_prove_destroy_succeeded(change):
    async def run():
        harness = ProbeHarness()

        async def final_read(keys):
            if change == "protocol":
                harness.api.protocol = SimpleNamespace(transport=object(), active=True)
            elif change == "transport":
                harness.protocol.transport = object()
            else:
                harness.protocol.active = False
            return [121]

        harness.api.fetch_component_states.side_effect = final_read
        result = await harness.run()
        assert result["same_socket"] is False
        assert result["result"] == "inconclusive"
        assert result["destroy_acknowledged"] is True
        assert result["reports_after"] == 0
        harness.assert_closed()

    asyncio.run(run())


@pytest.mark.parametrize(
    "baseline",
    [[], [(1, [101])], [(1, [101]), (1, [101])], [(1, [101]), (7, [108])]],
)
def test_no_changing_or_frequent_baseline_is_inconclusive(baseline):
    async def run():
        harness = ProbeHarness()
        harness.baseline = baseline
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["destroy_acknowledged"] is True
        assert result["reports_after"] == 0
        harness.assert_closed()

    asyncio.run(run())


@pytest.mark.parametrize("first_ack", [None, "3457", "private-state"])
def test_missing_or_wrong_ack_is_inconclusive_and_retries_only_own_id(first_ack, caplog):
    async def run():
        harness = ProbeHarness()
        harness.destroy_payloads = [first_ack, str(SUBSCRIPTION_ID)]
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["destroy_acknowledged"] is False
        assert result["cleanup_acknowledged"] is True
        assert [request.payload for request in harness.requests[1:]] == [
            "SYSTEM:clientDestroy(0,0,3456)",
            "SYSTEM:clientDestroy(0,0,3456)",
        ]
        assert "private-state" not in json.dumps(result) + caplog.text
        harness.assert_closed()

    asyncio.run(run())


@pytest.mark.parametrize("final_values", [[1], [100], [108], [MISSING_STATE], []])
def test_restarted_clu_or_failed_uptime_read_cannot_pass(final_values):
    async def run():
        harness = ProbeHarness()
        harness.api.fetch_component_states.return_value = final_values
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["uptime_did_not_decrease"] is False
        harness.assert_closed()

    asyncio.run(run())


def test_failed_final_ping_cannot_be_mistaken_for_successful_destruction():
    async def run():
        harness = ProbeHarness()
        harness.api.ping.side_effect = lambda: harness.clock < 21
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["clu_responsive_after"] is False
        harness.assert_closed()

    asyncio.run(run())


def test_stale_early_baseline_cannot_prove_report_cessation_after_destroy():
    async def run():
        harness = ProbeHarness()
        harness.baseline = [(1, [101]), (2, [102]), (3, [103])]
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["baseline_active"] is False
        assert result["destroy_acknowledged"] is True
        assert result["reports_after"] == 0
        harness.assert_closed()

    asyncio.run(run())


def test_failed_periodic_ping_prevents_passing_despite_final_ping_success():
    async def run():
        harness = ProbeHarness()
        harness.api.ping.side_effect = lambda: harness.clock != 10
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["clu_responsive_after"] is True
        assert result["keepalive_failures"] == 1
        harness.assert_closed()

    asyncio.run(run())


def test_registration_timeout_still_destroys_own_possibly_created_subscription():
    async def run():
        harness = ProbeHarness()
        harness.registration_payload = None
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["cleanup_acknowledged"] is True
        assert harness.requests[-1].payload == "SYSTEM:clientDestroy(0,0,3456)"
        harness.assert_closed()

    asyncio.run(run())


def test_malformed_registration_is_private_and_still_cleans_up(caplog):
    async def run():
        harness = ProbeHarness()
        harness.protocol.send_request.side_effect = [
            "resp:::private-state",
            "resp:192.0.2.1:1234abcd:3456",
        ]
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["reason"] == "Invalid registration response"
        assert result["cleanup_acknowledged"] is True
        assert "private-state" not in json.dumps(result) + caplog.text
        harness.assert_closed()

    asyncio.run(run())


def test_wrong_registration_id_cleans_up_only_expected_subscription():
    async def run():
        harness = ProbeHarness()
        harness.registration_payload = "clientReport:3457:{100}"
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["reason"] == "Unexpected registration response"
        assert harness.requests[-1].payload == "SYSTEM:clientDestroy(0,0,3456)"
        harness.assert_closed()

    asyncio.run(run())


def test_cancellation_during_baseline_destroys_subscription_and_closes_socket():
    async def run():
        harness = ProbeHarness()
        harness.cancel_baseline = True
        with pytest.raises(asyncio.CancelledError):
            await harness.run()
        assert harness.requests[-1].payload == "SYSTEM:clientDestroy(0,0,3456)"
        harness.assert_closed()

    asyncio.run(run())


def test_cancellation_while_registering_still_attempts_destroy():
    async def run():
        harness = ProbeHarness()
        harness.protocol.send_request.side_effect = [
            asyncio.CancelledError,
            "resp:192.0.2.1:1234abcd:3456",
        ]
        with pytest.raises(asyncio.CancelledError):
            await harness.run()
        request = harness.protocol.send_request.await_args.args[0]
        assert request.payload == "SYSTEM:clientDestroy(0,0,3456)"
        harness.assert_closed()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["connect", "ping"])
def test_initial_connection_failure_closes_without_registration(failure):
    async def run():
        harness = ProbeHarness()
        getattr(harness.api, failure).return_value = False
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert not harness.requests
        harness.assert_closed()

    asyncio.run(run())


def test_cleanup_failure_still_closes_socket():
    async def run():
        harness = ProbeHarness()
        harness.registration_payload = None
        harness.protocol.send_request.side_effect = [None, ConnectionError("closed")]
        result = await harness.run()
        assert result["result"] == "inconclusive"
        assert result["cleanup_acknowledged"] is False
        harness.assert_closed()

    asyncio.run(run())


def test_wrapper_suppresses_wire_debug_and_restores_logging_on_cancellation(caplog):
    async def run():
        harness = ProbeHarness()
        config = SimpleNamespace(clus=[harness.api.clu], encryption=object())
        original_level = probe._CLU_WIRE_LOGGER.level
        probe._CLU_WIRE_LOGGER.setLevel(logging.DEBUG)

        async def cancelled(api, seconds):
            probe._CLU_WIRE_LOGGER.debug("private-state")
            raise asyncio.CancelledError

        try:
            with (
                patch.object(probe, "load_configuration", return_value=config),
                patch.object(probe, "GrentonCluApi", return_value=harness.api),
                patch.object(probe, "verify_client_destroy", side_effect=cancelled),
                pytest.raises(asyncio.CancelledError),
            ):
                await probe.run_client_destroy_test(Path("unused"), "Main", 10)
            assert probe._CLU_WIRE_LOGGER.level == logging.DEBUG
            assert "private-state" not in caplog.text
        finally:
            probe._CLU_WIRE_LOGGER.setLevel(original_level)

    asyncio.run(run())


def test_command_dispatches_and_prints_inconclusive_result_before_failure():
    result = {"result": "inconclusive", "reason": "No changing baseline"}
    args = create_parser().parse_args(
        ["test-client-destroy", "--clu", "Main", "--observe-seconds", "10"]
    )
    output = io.StringIO()
    with (
        patch(
            "commands.client_destroy.run_client_destroy_test",
            new_callable=AsyncMock,
            return_value=result,
        ) as run,
        contextlib.redirect_stdout(output),
        pytest.raises(SystemExit) as failure,
    ):
        args.handler(args)
    assert failure.value.code == 1
    assert json.loads(output.getvalue()) == result
    assert run.await_args.args[1:] == ("Main", 10)


@pytest.mark.parametrize("seconds", ["4", "16", "-1"])
def test_invalid_window_is_rejected_before_loading_config(seconds):
    with (
        patch.object(probe, "load_configuration") as load,
        pytest.raises(SystemExit) as failure,
    ):
        create_parser().parse_args(["test-client-destroy", "--observe-seconds", seconds])
    assert failure.value.code == 2
    load.assert_not_called()
