"""HA Telnet ownership, log forwarding, errors and socket cleanup without a CLU."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_clu_scripts import make_entry, make_hass
from test_clu_state import coordinator, make_clu, register_platforms

from custom_components.homeassistant_grenton.domain.api.clu_messages.action import (
    GrentonCluApiActionRequest,
)
from custom_components.homeassistant_grenton.domain.entities.clu_diagnostics import (
    CluTelnetLoggingSwitch,
)
from custom_components.homeassistant_grenton.domain.entities.clu_state import (
    clu_entities,
)
from custom_components.homeassistant_grenton.integration_config import RuntimeData
from custom_components.homeassistant_grenton.state import MISSING_STATE
from custom_components.homeassistant_grenton.telnet_logging import (
    LOG_LOGGER_NAME,
    MAX_LINE_LENGTH,
    CluLogLines,
)

TRANSPORT = "custom_components.homeassistant_grenton.domain.api.telnet"


async def fixture(tmp_path, prefixes=("221",)):
    entry = make_entry()
    hass = await make_hass(tmp_path, [entry])
    clus = [make_clu(f"clu{i + 1}", prefix=prefix) for i, prefix in enumerate(prefixes)]
    coord = coordinator(hass, entry, clus)
    entities = [entity for clu in clus for entity in clu_entities(coord, clu)]
    entry.runtime_data = RuntimeData(coord, [], entities)
    switches = [
        entity for entity in entities if isinstance(entity, CluTelnetLoggingSwitch)
    ]
    for api in coord._apis.values():
        api.execute_action = AsyncMock(return_value=True)
        api.poll_component_states = AsyncMock(return_value=[4])
    return hass, entry, clus, coord, switches


def connection():
    reader = asyncio.StreamReader()
    writer = SimpleNamespace(
        write=Mock(),
        drain=AsyncMock(),
        close=Mock(),
        wait_closed=AsyncMock(),
    )
    return reader, writer


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("prefix,index", [("221", "31"), ("521", "23")])
def test_switch_confirms_debug_negotiates_logs_and_releases_socket(
    tmp_path,
    caplog,
    prefix,
    index,
):
    async def run():
        hass, entry, clus, coord, switches = await fixture(tmp_path, (prefix,))
        clu, switch = clus[0], switches[0]
        reader, writer = connection()
        reader.feed_data(b"\xff\xfd\x00")  # Captured DO BINARY acknowledgement.
        caplog.set_level(logging.WARNING, logger=LOG_LOGGER_NAME)
        caplog.handler.setLevel(logging.DEBUG)
        previous_level = logging.getLogger(LOG_LOGGER_NAME).level
        platforms = await register_platforms(hass, entry)
        try:
            assert not switch.is_on
            with patch(
                f"{TRANSPORT}.asyncio.open_connection",
                AsyncMock(return_value=(reader, writer)),
            ) as connect:
                await asyncio.gather(switch.async_turn_on(), switch.async_turn_on())
                assert switch.is_on
                assert hass.states.get(switch.entity_id).state == "on"
                connect.assert_awaited_once_with(clu.ip, 23)
                api = coord._apis[clu.id]
                api.execute_action.assert_awaited_once()
                action = api.execute_action.await_args.args[0]
                assert (
                    GrentonCluApiActionRequest.from_action(action).payload
                    == f"{clu.id}:set({index},4)"
                )
                assert [
                    key.name for key in api.poll_component_states.await_args.args[0]
                ] == [index]
                reader.feed_data(b"2026-11-02 20:28:30 [DEBUG] \xc5")
                await settle()
                reader.feed_data(b"\xbc\xc3\xb3\xc5\x82\xc4\x87 100%\r")
                await settle()
                reader.feed_data(
                    b"\n20:29:04 [INFO] ready\r\n[WARNING] caution\n[ERROR] fault\npartial"
                )
                await settle()
                await switch.async_turn_off()
                assert not switch.is_on
                assert hass.states.get(switch.entity_id).state == "off"
                # Turning off releases only TCP ownership, leaving native verbosity alone.
                api.execute_action.assert_awaited_once()
            records = [r for r in caplog.records if r.name == LOG_LOGGER_NAME]
            assert [r.levelno for r in records] == [10, 20, 30, 40, 20]
            assert "żółć 100%" in records[0].message
            assert all(f"[{clu.name} / {clu.id}]" in r.message for r in records)
            assert records[-1].message.endswith("partial")
            assert logging.getLogger(LOG_LOGGER_NAME).level == previous_level
            assert writer.write.call_args_list[0].args[0] == b"\xff\xfb\x00"
            assert any(call.args[0] == b"\r\n" for call in writer.write.call_args_list)
            writer.close.assert_called_once()
            writer.wait_closed.assert_awaited_once()
            assert not coord.telnet_logs._tasks
        finally:
            for platform in platforms:
                await platform.async_reset()
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["reset", "eof", "silent", "refused", "write_timeout"]
)
def test_rejected_session_fails_service_leaves_switch_off_without_retries(
    tmp_path,
    caplog,
    failure,
):
    async def run():
        hass, _entry, clus, coord, switches = await fixture(tmp_path)
        reader, writer = connection()
        if failure == "reset":
            reader.set_exception(ConnectionResetError("busy"))
        elif failure == "eof":
            reader.feed_eof()
        elif failure == "write_timeout":

            async def blocked():
                await asyncio.Future()

            writer.drain.side_effect = blocked
        connect = AsyncMock(
            side_effect=ConnectionRefusedError("refused")
            if failure == "refused"
            else None,
            return_value=(reader, writer),
        )
        try:
            with (
                patch(f"{TRANSPORT}.asyncio.open_connection", connect),
                patch(f"{TRANSPORT}.CONNECT_TIMEOUT", 0.01),
                pytest.raises(HomeAssistantError, match="Only one Telnet connection"),
            ):
                await switches[0].async_turn_on()
            assert not switches[0].is_on
            assert not coord.telnet_logs._tasks
            connect.assert_awaited_once()
            assert (
                len(
                    [
                        r
                        for r in caplog.records
                        if "Could not start Telnet logging" in r.message
                    ]
                )
                == 1
            )
            coord._apis[clus[0].id].execute_action.assert_awaited_once()
            if failure != "refused":
                writer.close.assert_called_once()
                writer.wait_closed.assert_awaited_once()
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("result", ["rejected", 2, MISSING_STATE])
def test_unconfirmed_debug_does_not_open_telnet(tmp_path, result):
    async def run():
        hass, _entry, clus, coord, switches = await fixture(tmp_path)
        api = coord._apis[clus[0].id]
        api.execute_action.return_value = result != "rejected"
        api.poll_component_states.return_value = [result]
        try:
            with patch(f"{TRANSPORT}.asyncio.open_connection", AsyncMock()) as connect:
                with pytest.raises(HomeAssistantError, match="rejected|confirm DEBUG"):
                    await switches[0].async_turn_on()
                connect.assert_not_awaited()
            assert not switches[0].is_on
            assert not coord.telnet_logs._tasks
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("reset", [False, True])
def test_disconnected_stream_switches_off_and_can_be_started_manually(
    tmp_path,
    caplog,
    reset,
):
    async def run():
        hass, _entry, clus, coord, switches = await fixture(tmp_path)
        first, writer = connection()
        second, next_writer = connection()
        first.feed_data(b"\xff\xfd\x00")
        second.feed_data(b"\xff\xfd\x00")
        try:
            with patch(
                f"{TRANSPORT}.asyncio.open_connection",
                AsyncMock(side_effect=[(first, writer), (second, next_writer)]),
            ) as connect:
                await switches[0].async_turn_on()
                task = coord.telnet_logs._tasks[clus[0].id]
                if reset:
                    first.set_exception(ConnectionResetError("restarted"))
                else:
                    first.feed_eof()
                await task
                assert not switches[0].is_on
                connect.assert_awaited_once()
                assert (
                    len(
                        [
                            r
                            for r in caplog.records
                            if "Telnet logging stopped" in r.message
                        ]
                    )
                    == 1
                )
                await switches[0].async_turn_on()
                assert switches[0].is_on and connect.await_count == 2
                await coord.async_shutdown()
                assert not switches[0].is_on
                writer.close.assert_called_once()
                next_writer.close.assert_called_once()
        finally:
            await coord.async_shutdown()
            await hass.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["debug", "connect", "handshake"])
def test_shutdown_cancels_pending_startup_and_closes_owned_socket(tmp_path, stage):
    async def run():
        hass, _entry, clus, coord, switches = await fixture(tmp_path)
        started = asyncio.Event()
        reader, writer = connection()

        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Future()

        if stage == "debug":
            coord._apis[clus[0].id].execute_action.side_effect = blocked
        elif stage == "handshake":
            reader.read = AsyncMock(side_effect=blocked)
        connect = AsyncMock(
            side_effect=blocked if stage == "connect" else None,
            return_value=(reader, writer),
        )
        with patch(f"{TRANSPORT}.asyncio.open_connection", connect):
            start = asyncio.create_task(switches[0].async_turn_on())
            await started.wait()
            await coord.async_shutdown()
            with pytest.raises(asyncio.CancelledError):
                await start
        assert not switches[0].is_on
        assert not coord.telnet_logs._tasks
        if stage == "handshake":
            writer.close.assert_called_once()
            writer.wait_closed.assert_awaited_once()
        await hass.async_stop()

    asyncio.run(run())


def test_two_clus_keep_logger_enabled_until_last_session_stops(tmp_path, caplog):
    async def run():
        hass, _entry, clus, coord, switches = await fixture(tmp_path, ("221", "521"))
        streams = [connection(), connection()]
        for reader, _writer in streams:
            reader.feed_data(b"\xff\xfd\x00")
        caplog.set_level(logging.ERROR, logger=LOG_LOGGER_NAME)
        caplog.handler.setLevel(logging.DEBUG)
        logger = logging.getLogger(LOG_LOGGER_NAME)
        with patch(
            f"{TRANSPORT}.asyncio.open_connection", AsyncMock(side_effect=streams)
        ):
            await asyncio.gather(*(switch.async_turn_on() for switch in switches))
            await switches[0].async_turn_off()
            assert not switches[0].is_on and switches[1].is_on
            assert logger.level == logging.DEBUG
            streams[1][0].feed_data(b"[DEBUG] second remains active\n")
            await settle()
            await coord.async_shutdown()
        assert logger.level == logging.ERROR
        assert any(
            f"{clus[1].id}] [DEBUG] second remains active" in r.message
            for r in caplog.records
        )
        await hass.async_stop()

    asyncio.run(run())


def test_unterminated_lines_are_bounded_and_control_bytes_do_not_leak(caplog):
    caplog.set_level(logging.DEBUG, logger=LOG_LOGGER_NAME)
    lines = CluLogLines(make_clu("clu1"))
    lines.feed("x" * (MAX_LINE_LENGTH * 2 + 3))
    assert len(lines.pending) == 3
    lines.feed("\x00\r\n")
    lines.feed("[WARN] warning\nuntagged\n")
    records = [r for r in caplog.records if r.name == LOG_LOGGER_NAME]
    assert [r.levelno for r in records] == [20, 20, 20, 30, 20]
    assert all("\x00" not in r.message and "\r" not in r.message for r in records)


def test_unsupported_controller_has_no_telnet_switch(tmp_path):
    async def run():
        hass, _entry, clus, coord, switches = await fixture(tmp_path, ("999",))
        assert not switches
        with pytest.raises(HomeAssistantError, match="does not support"):
            await coord.telnet_logs.async_start(clus[0].id)
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())


def test_cancelled_worker_before_first_step_does_not_leave_startup_pending(tmp_path):
    async def run():
        hass, _entry, clus, coord, switches = await fixture(tmp_path)
        create_task = asyncio.create_task

        def immediately_cancel(coroutine):
            task = create_task(coroutine)
            task.cancel()
            return task

        with patch(
            "custom_components.homeassistant_grenton.telnet_logging.asyncio.create_task",
            side_effect=immediately_cancel,
        ):
            async with asyncio.timeout(1):
                with pytest.raises(asyncio.CancelledError):
                    await switches[0].async_turn_on()
        assert not switches[0].is_on
        assert not coord.telnet_logs._tasks
        coord._apis[clus[0].id].execute_action.assert_not_awaited()
        await coord.async_shutdown()
        await hass.async_stop()

    asyncio.run(run())
