"""Verify destruction of one temporary uptime subscription on a live CLU."""

import asyncio
import logging
import secrets
from itertools import pairwise
from pathlib import Path
from time import monotonic

from config import load_configuration
from grenton_api import _CLU_WIRE_LOGGER, GrentonCluApi
from homeassistant_grenton.domain.api.clu_messages import (
    GrentonCluApiClientDestroyRequest,
    GrentonCluApiClientDestroyResponse,
    GrentonCluApiClientRegisterRequest,
    GrentonCluApiClientRegisterResponse,
)
from homeassistant_grenton.state import GrentonCluStateAttributeKey

_LOGGER = logging.getLogger(__name__)


async def _destroy(protocol, subscription_id: int) -> bool:
    try:
        wire = await protocol.send_request(
            GrentonCluApiClientDestroyRequest(subscription_id)
        )
    except ConnectionError:
        return False
    if wire is None:
        return False
    try:
        GrentonCluApiClientDestroyResponse(wire, subscription_id)
    except ValueError:
        _LOGGER.warning("clientDestroy did not return the expected subscription ID")
        return False
    return True


async def verify_client_destroy(api: GrentonCluApi, observe_seconds: int) -> dict:
    """Keep the registering socket open throughout both observation windows.

    No integration-owned endpoint is reused, and no object value is written.
    An acknowledgement is only supporting evidence: reports must stop while
    the CLU remains responsive and its uptime continues increasing.
    """
    if not 5 <= observe_seconds <= 15:
        raise ValueError("Observation window must be between 5 and 15 seconds")

    subscription_id = secrets.randbelow(65535 - 1024) + 1024
    reports: list[tuple[float, int | float]] = []
    key = GrentonCluStateAttributeKey(api.clu.object_name, "0")
    registration_attempted = False
    destroyed = False
    protocol = None
    keepalive_task = None
    keepalive_results: list[bool] = []
    ping_lock = asyncio.Lock()
    result = {
        "clu": {"name": api.clu.name, "id": api.clu.id},
        "subscription_id": subscription_id,
        "key": f"{key.object_name}.{key.name}",
        "observation_seconds": observe_seconds,
        "result": "inconclusive",
    }

    async def receive(values):
        if len(values) == 1 and type(values[0]) in (int, float):
            reports.append((monotonic(), values[0]))

    async def keepalive():
        while True:
            await asyncio.sleep(10)
            async with ping_lock:
                keepalive_results.append(await api.ping())

    async def stop_keepalive():
        if keepalive_task is not None:
            # Let an in-flight health check finish before cancelling its worker.
            async with ping_lock:
                keepalive_task.cancel()
                await asyncio.gather(keepalive_task, return_exceptions=True)

    try:
        if not await api.connect() or not await api.ping():
            result["reason"] = "CLU connection or initial ping failed"
            return result
        protocol = api.protocol
        if protocol is None:
            result["reason"] = "UDP socket unavailable"
            return result
        registered_transport = protocol.transport
        protocol.subscription_keys = [key]
        protocol.subscription_callback = receive
        registration_attempted = True
        wire = await protocol.send_request(
            GrentonCluApiClientRegisterRequest(
                [key], 0, subscription_id=subscription_id
            )
        )
        if wire is None:
            result["reason"] = "Subscription registration timed out"
            return result
        try:
            initial = GrentonCluApiClientRegisterResponse(wire, [key], api.clu)
        except ValueError:
            result["reason"] = "Invalid registration response"
            return result
        if initial.session_id != subscription_id or len(initial.values) != 1:
            result["reason"] = "Unexpected registration response"
            return result
        initial_uptime = initial.values[0]
        if type(initial_uptime) not in (int, float):
            result["reason"] = "Uptime was not numeric"
            return result

        keepalive_task = asyncio.create_task(keepalive())
        _LOGGER.info("Observing temporary uptime reports for %ds", observe_seconds)
        await asyncio.sleep(observe_seconds)
        before = list(reports)
        max_gap = max(
            (right[0] - left[0] for left, right in pairwise(before)),
            default=float("inf"),
        )
        result["reports_before"] = len(before)
        baseline_active = (
            len(before) >= 2
            and all(right[1] > left[1] for left, right in pairwise(before))
            and before[0][1] >= initial_uptime
            and monotonic() - before[-1][0] <= max(2, 2 * max_gap)
        )
        result["baseline_active"] = baseline_active
        _LOGGER.info("Sending clientDestroy for the temporary subscription")
        destroyed = await _destroy(protocol, subscription_id)
        result["destroy_acknowledged"] = destroyed

        # Reports already queued when the acknowledgement arrives may still
        # be delivered. Keep their count separate from the verification window.
        at_ack = len(reports)
        await asyncio.sleep(1)
        after_grace = len(reports)
        result["reports_during_grace"] = after_grace - at_ack
        _LOGGER.info("Checking for reports after destruction for %ds", observe_seconds)
        await asyncio.sleep(observe_seconds)
        await stop_keepalive()
        result["clu_responsive_after"] = await api.ping()
        result["keepalive_failures"] = sum(not reply for reply in keepalive_results)
        values = await api.fetch_component_states([key])
        await asyncio.sleep(0)
        result["reports_after"] = len(reports) - after_grace
        result["same_socket"] = (
            api.protocol is protocol
            and protocol.active
            and protocol.transport is registered_transport
        )
        final_uptime = values[0] if len(values) == 1 else None
        result["uptime_did_not_decrease"] = (
            type(final_uptime) in (int, float)
            and final_uptime > initial_uptime
            and (not reports or final_uptime >= reports[-1][1])
        )
        sufficient_window = observe_seconds >= max(2, 3 * max_gap)
        result["sufficient_observation_window"] = sufficient_window
        if not result["same_socket"]:
            result["reason"] = "The original subscription socket closed or was replaced"
        elif result["clu_responsive_after"] and not result["uptime_did_not_decrease"]:
            result["reason"] = "Uptime read failed or the CLU restarted"
        elif not baseline_active or not sufficient_window:
            result["reason"] = "Insufficient changing reports before destruction"
        elif result["reports_after"]:
            result["result"] = "failed"
            result["reason"] = "Reports continued after clientDestroy"
        elif (
            not destroyed
            or not result["clu_responsive_after"]
            or result["keepalive_failures"]
        ):
            result["reason"] = "Destruction acknowledgement or health check missing"
        else:
            result["result"] = "passed"
        return result
    finally:
        try:
            await stop_keepalive()
            # Also clean up when registration replied late, parsing failed, or
            # Ctrl+C interrupted the baseline. Never destroy another session.
            if registration_attempted and protocol is not None and not destroyed:
                result["cleanup_acknowledged"] = await _destroy(protocol, subscription_id)
        finally:
            await api.disconnect()


async def run_client_destroy_test(
    config_path: Path, clu_name: str, observe_seconds: int = 10
) -> dict:
    """Load the usual CLI configuration and suppress private packet contents."""
    config = load_configuration(config_path)
    if not config:
        raise SystemExit(1)
    clu = next((clu for clu in config.clus if clu.name == clu_name), None)
    if clu is None:
        _LOGGER.error("CLU '%s' not found in configuration", clu_name)
        raise SystemExit(1)
    original_level = _CLU_WIRE_LOGGER.level
    if _CLU_WIRE_LOGGER.getEffectiveLevel() < logging.INFO:
        _CLU_WIRE_LOGGER.setLevel(logging.INFO)
    try:
        return await verify_client_destroy(GrentonCluApi(clu, config.encryption), observe_seconds)
    finally:
        _CLU_WIRE_LOGGER.setLevel(original_level)
