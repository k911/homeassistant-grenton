"""Renewal settings validate, persist and change live without replacing sockets."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import InvalidData
from probatio import Invalid
from test_clu_renewal_timing import fixture, tick
from test_clu_subscription_settings import open_settings
from test_integration_configuration import fields

from custom_components.homeassistant_grenton.clu_settings import (
    registration_interval, validate_registration_interval,
)
from custom_components.homeassistant_grenton.domain.state_object import GrentonVariableValueObject


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.5, 61, "55", float("nan"), float("inf")])
def test_invalid_intervals_reject_and_old_entries_fall_back(value):
    with pytest.raises(Invalid):
        validate_registration_interval(value)
    assert registration_interval({"clu_read_settings": {"clu1": {"registration_interval": value}}}, "clu1") == 45


@pytest.mark.parametrize("value", [1, 45, 55.0, 60])
def test_valid_intervals(value):
    assert validate_registration_interval(value) == value
    assert type(validate_registration_interval(value)) is int


def test_zwave_dialog_defaults_to_45_and_saves_60_without_reload(tmp_path):
    async def run():
        runtime = SimpleNamespace(
            async_set_subscription_id=AsyncMock(),
            async_set_slow_read_interval=AsyncMock(),
            async_set_registration_interval=AsyncMock(),
        )
        original = {"clu_read_settings": {"clu2": {"slow_read_interval": 120}}}
        hass, entry, manager, form = await open_settings(tmp_path, original, runtime, zwave=True)
        field = fields(form)["registration_interval"]
        assert field["default"] == 45
        assert field["selector"]["number"]["max"] == 60
        result = await manager.async_configure(form["flow_id"], {
            "slow_read_interval": 60, "subscription_id": 1, "registration_interval": 60,
        })
        assert result["type"] == "create_entry"
        assert entry.options["clu_read_settings"]["clu1"]["registration_interval"] == 60
        assert entry.options["clu_read_settings"]["clu2"] == original["clu_read_settings"]["clu2"]
        runtime.async_set_registration_interval.assert_awaited_once_with("clu1", 60)
        runtime.async_set_slow_read_interval.assert_not_awaited()
        hass.config_entries.async_schedule_reload.assert_not_called()
        await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("interval", [61, 55.5])
def test_invalid_interval_does_not_change_runtime_or_options(tmp_path, interval):
    async def run():
        runtime = SimpleNamespace(
            async_set_subscription_id=AsyncMock(),
            async_set_registration_interval=AsyncMock(),
        )
        hass, entry, manager, form = await open_settings(tmp_path, runtime=runtime, zwave=True)
        submission = {
            "slow_read_interval": 60, "subscription_id": 69, "registration_interval": interval,
        }
        if interval > 60:
            with pytest.raises(InvalidData):
                await manager.async_configure(form["flow_id"], submission)
        else:
            result = await manager.async_configure(form["flow_id"], submission)
            assert result["errors"] == {"registration_interval": "invalid_configuration"}
        assert entry.options == {}
        runtime.async_set_subscription_id.assert_not_awaited()
        runtime.async_set_registration_interval.assert_not_awaited()
        manager.async_abort(form["flow_id"])
        await hass.async_stop()
    asyncio.run(run())


def test_interval_change_wakes_only_its_timer_and_updates_freshness(tmp_path):
    async def run():
        hass, coord, _ = await fixture(tmp_path, count=2)
        state = GrentonVariableValueObject("clu1", "", "live")
        coord.register_component_state(state)
        key = coord._state_key(state)
        coord._realtime_success["clu1"][key] = 0
        renewed = asyncio.Event()
        coord._send_register = AsyncMock(side_effect=lambda _: renewed.set())
        sockets = {clu_id: api.protocol for clu_id, api in coord._apis.items()}
        task = asyncio.create_task(coord._periodic_clu_loop("clu1", 45, register=True))
        try:
            await tick()
            await coord.async_set_registration_interval("clu1", 55)
            await asyncio.wait_for(renewed.wait(), 0.5)
            assert coord._registration_intervals == {"clu1": 55, "clu2": 45}
            assert not coord._registration_changed["clu2"].is_set()
            assert sockets == {clu_id: api.protocol for clu_id, api in coord._apis.items()}
            with patch("custom_components.homeassistant_grenton.coordinator.monotonic", return_value=109.9):
                assert coord._realtime_key_fresh("clu1", key)
            with patch("custom_components.homeassistant_grenton.coordinator.monotonic", return_value=110):
                assert not coord._realtime_key_fresh("clu1", key)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await coord.async_shutdown()
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("clu_id", ["clu1", "clu2"])
def test_other_family_dialog_keeps_existing_fields(tmp_path, clu_id):
    async def run():
        hass, _, manager, form = await open_settings(tmp_path, clu_id=clu_id)
        assert "registration_interval" not in fields(form)
        manager.async_abort(form["flow_id"])
        await hass.async_stop()
    asyncio.run(run())


def test_change_during_renewal_does_not_cancel_the_request(tmp_path):
    async def run():
        hass, coord, _ = await fixture(tmp_path)
        coord.register_component_state(GrentonVariableValueObject("clu1", "", "live"))
        entered, release, renewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancelled = False

        async def register(_):
            nonlocal cancelled
            if not entered.is_set():
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled = True
                    raise
            else:
                renewed.set()

        coord._send_register = register
        coord._registration_changed["clu1"].set()
        task = asyncio.create_task(coord._periodic_clu_loop("clu1", 45, register=True))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            await coord.async_set_registration_interval("clu1", 55)
            await tick()
            assert not cancelled
            assert not task.done()
            release.set()
            await asyncio.wait_for(renewed.wait(), 0.5)
            assert not cancelled
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await coord.async_shutdown()
            await hass.async_stop()
    asyncio.run(run())
