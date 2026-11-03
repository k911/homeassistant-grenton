"""Bounded, read-only verification of multiplexed CLU subscriptions."""

import asyncio
import logging
import secrets
from collections import Counter
from pathlib import Path

from config import load_configuration
from grenton_api import _CLU_WIRE_LOGGER, GrentonCluApi
from homeassistant_grenton.domain.api.clu_health import (
    KEEPALIVE_INTERVAL,
    REGISTRATION_INTERVAL,
)
from homeassistant_grenton.domain.api.clu_messages import (
    GrentonCluApiClientDestroyRequest,
    GrentonCluApiClientDestroyResponse,
    GrentonCluApiClientRegisterRequest,
    GrentonCluApiPingRequest,
)
from homeassistant_grenton.state import GrentonCluStateAttributeKey, MissingState

_LOGGER = logging.getLogger(__name__)


async def _periodic(interval, operation):
    """Run on deadlines without replaying missed ticks."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + interval
    while True:
        await asyncio.sleep(max(0, deadline - loop.time()))
        await operation()
        deadline += interval
        if deadline <= loop.time():
            deadline = loop.time() + interval


async def _observe(seconds):
    while seconds:
        window = min(20, seconds)
        await asyncio.sleep(window)
        seconds -= window
        if seconds:
            _LOGGER.info("Observation continues: %ds remaining", seconds)


async def verify_shared_subscriptions(api: GrentonCluApi, duration: int = 100) -> dict:
    """Observe two changing native counters across at least two renewals.

    Attribute 6 is the CLU's changing clock counter in the captured OM monitor.
    Isolating it locally forces two tiny groups without creating object values
    or modifying the controller. All output is counts and verification flags.
    """
    if not 100 <= duration <= 300:
        raise ValueError("Observation duration must be between 100 and 300 seconds")
    keys = [GrentonCluStateAttributeKey(api.clu.object_name, index) for index in ("0", "6")]
    api.large_value_keys.add(keys[1])
    counts = Counter()
    changes = Counter()
    last_values = {}
    trace = Counter()
    tasks = []
    result = {"clu": api.clu.name, "result": "inconclusive", "duration_seconds": duration}

    async def report(report_keys, values):
        if len(report_keys) != 1 or report_keys[0] not in keys or len(values) != 1:
            trace["routing_errors"] += 1
            return
        key, value = report_keys[0], values[0]
        counts[key] += 1
        if key in last_values and last_values[key] != value:
            changes[key] += 1
        last_values[key] = value

    async def ping():
        trace["ping_cycles"] += 1
        if not await api.ping():
            trace["ping_failures"] += 1

    async def renew():
        values = await api.register_component_states(keys)
        trace["renewals"] += 1
        if values is None or len(values) != 2 or any(isinstance(value, MissingState) for value in values):
            trace["renewal_failures"] += 1

    try:
        if not await api.connect() or not await api.ping():
            result["reason"] = "CLU connection or initial health ping failed"
            return result
        control = api.protocol
        send_request = control.send_request

        async def traced_request(request):
            if isinstance(request, GrentonCluApiClientRegisterRequest):
                trace["register_requests"] += 1
            elif isinstance(request, GrentonCluApiPingRequest):
                trace["ping_requests"] += 1
            elif isinstance(request, GrentonCluApiClientDestroyRequest):
                trace["destroy_requests"] += 1
            wire = await send_request(request)
            if isinstance(request, GrentonCluApiClientDestroyRequest) and wire is not None:
                try:
                    GrentonCluApiClientDestroyResponse(wire, request.subscription_id)
                except ValueError:
                    pass
                else:
                    trace["destroy_acknowledgements"] += 1
            return wire

        control.send_request = traced_request
        api.on_subscription_report = report
        initial = await api.register_component_states(keys)
        if initial is None or len(initial) != 2 or any(isinstance(value, MissingState) for value in initial):
            result["reason"] = "Initial subscription registration failed"
            return result
        initial_uptime = initial[0]
        if type(initial_uptime) not in (int, float):
            result["reason"] = "Initial uptime was not numeric"
            return result
        listener = api.report_protocol
        listener_transport = api.report_transport
        initial_ids = [group.subscription_id for group in api._groups]
        result["initial_ids"] = initial_ids
        result["consecutive_ids"] = initial_ids == [api.subscription_id, api.subscription_id + 1]
        tasks = [
            asyncio.create_task(_periodic(KEEPALIVE_INTERVAL, ping)),
            asyncio.create_task(_periodic(REGISTRATION_INTERVAL, renew)),
        ]
        _LOGGER.info("Observing two shared-listener groups for %ds", duration)
        await _observe(duration)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
        result["reports_before_rebase"] = [counts[key] for key in keys]
        result["changes_before_rebase"] = [changes[key] for key in keys]

        new_base = api.subscription_id + 10
        _LOGGER.info("Changing the temporary initial ID and observing both groups")
        await api.async_set_subscription_id(new_base)
        await renew()
        result["rebased_ids"] = [group.subscription_id for group in api._groups]
        before = counts.copy()
        await _observe(10)
        await ping()
        result["reports_after_rebase"] = [counts[key] - before[key] for key in keys]
        result["same_control_socket"] = api.protocol is control and control.active
        result["same_listener"] = (
            api.report_protocol is listener and api.report_transport is listener_transport
        )

        _LOGGER.info("Removing both groups; checking incoming datagrams on the open listener")
        await api.register_component_states([])
        await asyncio.sleep(1)
        raw_before = listener._received_report_datagrams
        await _observe(10)
        await ping()
        await asyncio.sleep(0)
        result["reports_after_cleanup"] = listener._received_report_datagrams - raw_before
        result["groups_after_cleanup"] = api.subscription_count
        result["listener_open_after_cleanup"] = listener.active
        final = await api.fetch_component_states([keys[0]])
        result["uptime_increased"] = (
            len(final) == 1 and type(final[0]) in (int, float) and final[0] > initial_uptime
        )
        result["traffic"] = dict(trace)
        result["result"] = "passed" if (
            result["consecutive_ids"]
            and result["rebased_ids"] == [new_base, new_base + 1]
            and all(count >= 2 for count in result["changes_before_rebase"])
            and all(count >= 2 for count in result["reports_after_rebase"])
            and trace["renewals"] >= 3
            and not trace["renewal_failures"]
            and not trace["ping_failures"]
            and not trace["routing_errors"]
            and trace["destroy_requests"] == trace["destroy_acknowledgements"] == 4
            and result["same_control_socket"] and result["same_listener"]
            and result["listener_open_after_cleanup"]
            and result["groups_after_cleanup"] == result["reports_after_cleanup"] == 0
            and result["uptime_increased"]
        ) else "failed"
        return result
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await api.disconnect()
        result["local_sockets_closed"] = api.transport is None and api.report_transport is None


async def run_shared_subscription_test(config_path: Path, clu_name: str, duration: int = 100):
    config = load_configuration(config_path)
    if not config:
        raise SystemExit(1)
    clu = next((clu for clu in config.clus if clu.name == clu_name), None)
    if clu is None:
        raise ValueError("CLU not found in the saved configuration")
    original_level = _CLU_WIRE_LOGGER.level
    _CLU_WIRE_LOGGER.setLevel(max(logging.INFO, _CLU_WIRE_LOGGER.getEffectiveLevel()))
    try:
        api = GrentonCluApi(clu, config.encryption, subscription_id=secrets.randbelow(50000) + 1024)
        return await verify_shared_subscriptions(api, duration)
    finally:
        _CLU_WIRE_LOGGER.setLevel(original_level)
