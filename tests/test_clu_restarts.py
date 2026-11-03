"""Restart observations and opt-in dump collection, without any live CLU traffic."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from test_clu_scripts import make_entry, make_hass
from test_clu_state import coordinator, make_clu
from test_clu_variables import set_options

from custom_components.homeassistant_grenton.domain.entities.clu_diagnostics import (
    CluDebugCapture,
    CluLastRestart,
)
from custom_components.homeassistant_grenton.domain.entities.clu_state import (
    GrentonCluUptime,
)
from custom_components.homeassistant_grenton.restarts import (
    CONF_DEBUG_CAPTURE,
    CluRestartMonitor,
    _save_dump,
)
from custom_components.homeassistant_grenton.state import (
    MISSING_STATE,
    GrentonCluStateAttributeKey,
)


async def fixture(tmp_path, enabled=False, interval=60):
    entry = make_entry()
    set_options(
        entry, {CONF_DEBUG_CAPTURE: {"clu1": enabled}, "other_option": "preserve"}
    )
    hass = await make_hass(tmp_path, [entry])
    hass.config_entries.async_update_entry = Mock(
        side_effect=lambda entry, options: set_options(entry, options)
    )
    clu = make_clu("clu1")
    coord = coordinator(hass, entry, [clu])
    coord._slow_read_intervals[clu.id] = interval
    return hass, entry, clu, coord


@pytest.mark.parametrize(
    "seconds,interval,detected",
    [(65, 60, True), (66, 60, False), (15, 10, True), (16, 10, False), (0, 60, True)],
)
def test_startup_window_and_estimated_boot_time(
    tmp_path, caplog, seconds, interval, detected
):
    async def run():
        hass, _entry, clu, coord = await fixture(tmp_path, interval=interval)
        now = datetime(2026, 11, 3, 12, tzinfo=UTC)
        key = GrentonCluStateAttributeKey(clu.id, "0")
        with patch(
            "custom_components.homeassistant_grenton.restarts.datetime"
        ) as clock:
            clock.now.return_value = now
            coord.restarts.observe(clu.id, [key], [seconds])
            coord.restarts.observe(clu.id, [key], [seconds])
            coord.restarts.observe(clu.id, [key], [seconds + 1])
        record = coord.restarts.records[clu.id]
        assert record.boot_time == now - timedelta(seconds=seconds)
        assert (record.detected_at is not None) is detected
        assert sum("restart" in rec.message for rec in caplog.records) == int(detected)
        sensor = CluLastRestart(coord, clu, "clu_last_restart")
        assert sensor.native_value == record.boot_time
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_invalid_samples_do_not_replace_baseline_or_emit_repeated_warning(
    tmp_path, caplog
):
    async def run():
        hass, _entry, clu, coord = await fixture(tmp_path)
        key = GrentonCluStateAttributeKey(clu.id, "0")
        coord.restarts.observe(clu.id, [key], [220104])
        for value in (
            MISSING_STATE,
            None,
            True,
            -1,
            1.5,
            "bad",
            float("nan"),
            float("inf"),
            10**100,
        ):
            coord.restarts.observe(clu.id, [key], [value])
            assert coord.restarts.last_uptime[clu.id] == 220104
        coord.restarts.observe(
            clu.id, [key], [200]
        )  # Beyond startup window, still a drop.
        coord.restarts.observe(clu.id, [key], [200])
        coord.restarts.observe(clu.id, [key], [201])
        assert len([r for r in caplog.records if "CLU restart" in r.message]) == 1
        assert coord.restarts.records[clu.id].previous_uptime == 220104
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_poll_and_push_share_baseline_ignore_unrelated_objects(tmp_path, caplog):
    async def run():
        hass, _entry, clu, coord = await fixture(tmp_path)
        uptime = GrentonCluUptime(coord, clu)
        api = coord._apis[clu.id]
        api.poll_component_states = AsyncMock(return_value=[1000])
        await coord._send_slow_poll(clu.id)
        await coord._process_report(
            clu.id, [GrentonCluStateAttributeKey("OTHER", "0")], [0]
        )
        assert coord.restarts.last_uptime[clu.id] == 1000
        key = coord._state_key(uptime.state_object)
        await coord._process_report(clu.id, [key], [12])
        api.poll_component_states.return_value = [13]
        await coord._send_slow_poll(clu.id)
        assert sum("CLU restart" in r.message for r in caplog.records) == 1
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "results,success",
    [
        ([OSError("absent"), b"dump"], True),
        ([b"", b""], False),
        ([OSError("one"), OSError("two")], False),
    ],
)
def test_exactly_two_attempts_and_final_warning(tmp_path, caplog, results, success):
    async def run():
        hass, _entry, clu, coord = await fixture(tmp_path, True)
        key = GrentonCluStateAttributeKey(clu.id, "0")
        with (
            patch(
                "custom_components.homeassistant_grenton.restarts.download_file",
                AsyncMock(side_effect=results),
            ) as download,
            patch(
                "custom_components.homeassistant_grenton.restarts.CAPTURE_RETRY_DELAY",
                0,
            ),
        ):
            coord.restarts.observe(clu.id, [key], [5])
            await coord.restarts._captures[clu.id]
        assert download.await_count == 2
        assert all(call.args[2] == r"a:\debug.bin" for call in download.await_args_list)
        record = coord.restarts.records[clu.id]
        assert record.capture_status == ("saved" if success else "failed")
        assert sum(
            "both download attempts" in r.message for r in caplog.records
        ) == int(not success)
        if success:
            assert Path(hass.config.path(record.capture_file)).read_bytes() == b"dump"
        else:
            assert not list(tmp_path.rglob("*.bin"))
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_default_disabled_and_independent_persistent_switch(tmp_path):
    async def run():
        hass, entry, clu, coord = await fixture(tmp_path)
        switch = CluDebugCapture(coord, clu, "clu_debug_capture")
        api = coord._apis[clu.id]
        api.execute_action = AsyncMock()
        assert switch.is_on is False
        with patch(
            "custom_components.homeassistant_grenton.restarts.download_file",
            AsyncMock(),
        ) as download:
            coord.restarts.observe(
                clu.id, [GrentonCluStateAttributeKey(clu.id, "0")], [5]
            )
            await asyncio.sleep(0)
            download.assert_not_awaited()
        await switch.async_turn_on()
        assert switch.is_on is True and entry.options["other_option"] == "preserve"
        api.execute_action.assert_not_awaited()
        restored = CluRestartMonitor(
            hass, entry, [clu], coord.encryption, coord._slow_read_intervals, Mock()
        )
        assert restored.capture_enabled(clu.id) is True
        assert restored.last_uptime == {}  # Preference persists; detection does not.
        await switch.async_turn_off()
        assert switch.is_on is False
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_success_first_attempt_no_retry_and_shutdown_cancels_download(tmp_path):
    async def run():
        hass, _entry, clu, coord = await fixture(tmp_path, True)
        key = GrentonCluStateAttributeKey(clu.id, "0")
        with patch(
            "custom_components.homeassistant_grenton.restarts.download_file",
            AsyncMock(return_value=b"dump"),
        ) as download:
            coord.restarts.observe(clu.id, [key], [20])
            await coord.restarts._captures[clu.id]
            download.assert_awaited_once()
        started = asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Future()

        with patch(
            "custom_components.homeassistant_grenton.restarts.download_file",
            side_effect=blocked,
        ) as download:
            coord.restarts.observe(clu.id, [key], [0])
            await started.wait()
            await coord.async_shutdown()
            download.assert_awaited_once()
            assert not coord.restarts._tasks
        await hass.async_stop()

    asyncio.run(run())


def test_dump_retention_only_removes_old_successful_dumps(tmp_path):
    for i in range(7):
        _save_dump(tmp_path, f"debug-{i:02d}.bin", bytes([i]))
    assert [p.name for p in sorted(tmp_path.glob("*.bin"))] == [
        f"debug-{i:02d}.bin" for i in range(2, 7)
    ]
    (tmp_path / "unrelated.bin").write_bytes(b"keep")
    with (
        patch(
            "custom_components.homeassistant_grenton.restarts.os.replace",
            side_effect=OSError("disk"),
        ),
        pytest.raises(OSError),
    ):
        _save_dump(tmp_path, "debug-99.bin", b"new")
    assert (tmp_path / "unrelated.bin").read_bytes() == b"keep"
    assert len(list(tmp_path.glob("debug-*.bin"))) == 5
    assert not list(tmp_path.glob(".debug-*"))


def test_turning_off_capture_cancels_retry_without_touching_log_level(tmp_path):
    async def run():
        hass, _entry, clu, coord = await fixture(tmp_path, True)
        started = asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Future()

        with patch(
            "custom_components.homeassistant_grenton.restarts.download_file",
            side_effect=blocked,
        ) as download:
            coord.restarts.observe(
                clu.id, [GrentonCluStateAttributeKey(clu.id, "0")], [5]
            )
            await started.wait()
            switch = CluDebugCapture(coord, clu, "clu_debug_capture")
            await switch.async_turn_off()
            assert switch.is_on is False
            assert coord.restarts.records[clu.id].capture_status == "disabled"
            assert coord.restarts._captures[clu.id].done()
            download.assert_awaited_once()
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_runtime_interval_change_and_controller_identity_are_respected(tmp_path):
    async def run():
        hass, entry, clu, coord = await fixture(tmp_path)
        second = make_clu("clu2", prefix="521")
        intervals = {clu.id: 60, second.id: 10}
        monitor = CluRestartMonitor(
            hass, entry, [clu, second], coord.encryption, intervals, Mock()
        )
        monitor.observe(clu.id, [GrentonCluStateAttributeKey(clu.id, "0")], [66])
        monitor.observe(second.id, [GrentonCluStateAttributeKey(second.id, "0")], [16])
        assert all(r.detected_at is None for r in monitor.records.values())
        assert monitor.last_uptime == {clu.id: 66, second.id: 16}
        monitor.observe(second.id, [GrentonCluStateAttributeKey(clu.id, "0")], [0])
        assert monitor.last_uptime[second.id] == 16
        # A replacement coordinator uses the current interval, no restored uptime.
        intervals[clu.id] = 120
        fresh = CluRestartMonitor(
            hass, entry, [clu, second], coord.encryption, intervals, Mock()
        )
        fresh.observe(clu.id, [GrentonCluStateAttributeKey(clu.id, "0")], [66])
        assert fresh.records[clu.id].detected_at is not None
        assert fresh.records[second.id].boot_time is None
        await monitor.async_shutdown()
        await fresh.async_shutdown()
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())
