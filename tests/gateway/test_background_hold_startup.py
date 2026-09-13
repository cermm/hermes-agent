"""Executable held-start boundaries; transports/providers stop at disposable I/O seams."""
import asyncio
import atexit
import json
import os
import socket
import struct
import threading
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import AsyncMock, Mock

import pytest

from agent import estop
from cron import scheduler_provider
from gateway import run as gateway_run
from gateway import run_startup
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.run_goals import GatewayGoalsMixin
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_startup import GatewayStartupMixin


class EffectReached(AssertionError):
    pass


def forbidden(*args, **kwargs) -> NoReturn:
    raise EffectReached("autonomous effect boundary")


class Adapter:
    """Use the real connectivity property with an entirely in-memory transport."""
    is_connected = BasePlatformAdapter.is_connected
    send_path_degraded = BasePlatformAdapter.send_path_degraded

    def __init__(self):
        self._running = False

        self.events = []
        self.receive: Any = None

    async def connect(self, *, is_reconnect=False):
        self._running = True
        return True

    async def disconnect(self):
        self._running = False

    async def send(self, *args, **kwargs):
        forbidden()

    async def get_chat_info(self, chat_id):
        forbidden()

    async def handle_message(self, event):
        self.events.append(event)
        return await self.receive(event)


class Runner(GatewayStartupMixin, GatewayGoalsMixin, GatewayInboundMixin):
    pass


@pytest.fixture(autouse=True)
def isolated_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(estop, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(estop, "_canonical_root", lambda: tmp_path)
    # Even a misplaced spy may never reach external transport or a subprocess.
    original_connect = socket.socket.connect

    def local_only(sock, address):
        if sock.family != socket.AF_UNIX:
            forbidden()
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    return tmp_path


@pytest.fixture
def runner(monkeypatch):
    r: Any = Runner()
    adapter = Adapter()
    r.adapters = {}
    r._failed_platforms = {}
    r._profile_adapters = {}
    r.config = SimpleNamespace(platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True)},
                               multiplex_profiles=False, token="secret-config-canary")
    r.delivery_router = SimpleNamespace(adapters={})
    r._start_install_faulthandler = Mock()
    r._start_log_startup_environment = Mock()
    r._abort_startup_if_shutdown_requested = AsyncMock(return_value=False)
    r._startup_should_abort = lambda: False
    r._start_check_access_policy = lambda: False
    r._multiplex_on = lambda: False
    r._create_adapter = lambda *a: adapter
    r._wire_adapter_handlers = Mock()
    r._connect_initial_adapter_with_timeout = lambda adp, p: adp.connect()
    r._update_platform_runtime_status = Mock()
    r._publish_primary_adapter = lambda p, adp: r.adapters.__setitem__(p, adp)
    r._start_secondary_profile_adapters = AsyncMock(return_value=0)
    r._wire_teams_pipeline_runtime = Mock()
    r._install_plugin_message_injector = Mock()
    r._update_runtime_status = Mock()
    r._adapter_for_source = lambda source: adapter
    r._scale_to_zero_note_real_inbound = Mock()
    r._hm_pre_gateway_dispatch_hook = lambda event, source: event
    r._is_user_authorized_for_source = lambda source: True
    adapter.receive = r._hm_admit_event
    r.effects = {}
    async_names = (
        "_start_recover_previous_run", "_claim_pending_obligations", "_run_startup_resume_event",
        "_send_restart_notification", "_send_home_channel_startup_notifications",
        "_redeliver_claimed_obligations", "_start_post_connect_services",
        "_send_session_db_warning_notifications", "_warm_goals_session_db",
    )
    sync_names = ("_start_startup_warmup", "_schedule_resume_pending_sessions", "_spawn_supervised",
                  "_spawn_reconnect_watcher")
    for name in async_names:
        spy = AsyncMock(side_effect=forbidden)
        setattr(r, name, spy)
        r.effects[name] = spy
    for name in sync_names:
        spy = Mock(side_effect=forbidden)
        setattr(r, name, spy)
        r.effects[name] = spy
    # Watcher callbacks must not even be scheduled (including cleanup/catalog/Kanban).
    for name in (*r._PRE_RECONNECT_WATCHERS, *r._POST_RECONNECT_WATCHERS,
                 "_drain_control_watcher", "_run_process_watcher"):
        setattr(r, name, AsyncMock(side_effect=forbidden))
    heartbeat = asyncio.Event()

    async def health_only(**kwargs):
        heartbeat.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(run_startup, "loop_heartbeat_forever", health_only)
    r.health_seen = heartbeat
    r.fake_adapter = adapter
    return r


async def stop_health(r):
    task = getattr(r, "_loop_heartbeat_task", None)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def event():
    return SimpleNamespace(source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="test-chat",
                           user_id="test-user", profile=None), internal=False, text="explicit chat")


@pytest.mark.asyncio
async def test_real_held_start_opens_chat_and_keeps_only_lifecycle_heartbeat(runner, isolated_boundaries):
    (isolated_boundaries / "BACKGROUND_HOLD").touch()
    # A real message arriving while adapters connect must queue and then replay.
    original_connect = runner._connect_initial_adapter_with_timeout
    inbound = event()

    async def connecting(adp, p):
        ok = await original_connect(adp, p)
        assert await adp.handle_message(inbound) is None
        assert runner._startup_restore_queue == [inbound]
        return ok

    runner._connect_initial_adapter_with_timeout = connecting
    try:
        assert await runner.start() is True
        await asyncio.wait_for(runner.health_seen.wait(), 3)
        assert runner.fake_adapter.is_connected is True
        assert runner._startup_restore_in_progress is False
        assert runner._startup_restore_queue == []
        admitted = await runner.fake_adapter.handle_message(event())
        assert admitted is not None and admitted[2] is False
        assert runner._hm_estop_gate(admitted[0], admitted[1], False) is None
        for spy in runner.effects.values():
            spy.assert_not_called()
        # Real explicit tool code is not globally disabled by the background marker.
        from tools.todo_tool import TodoStore, todo_tool
        result = json.loads(todo_tool(store=TodoStore()))
        assert "error" not in result
    finally:
        await stop_health(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("global_pause", [False, True])
async def test_unheld_start_preserves_wiring_even_with_global_estop(runner, isolated_boundaries, global_pause):
    if global_pause:
        (isolated_boundaries / "ESTOP").touch()
    runner._start_recover_previous_run = AsyncMock()
    runner._start_startup_warmup = Mock()
    runner._start_finish_wiring = AsyncMock()
    runner._start_spawn_background_watchers = Mock()
    assert await runner.start() is True
    runner._start_recover_previous_run.assert_awaited_once()
    runner._start_startup_warmup.assert_called_once()
    runner._start_finish_wiring.assert_awaited_once_with(1)
    runner._start_spawn_background_watchers.assert_called_once()
    if global_pause:
        assert estop.paused_reply() is not None
        (isolated_boundaries / "ESTOP").unlink()
        assert estop.paused_reply() is None


@pytest.mark.asyncio
async def test_held_start_latches_skipped_cron_across_marker_removal(runner, isolated_boundaries, monkeypatch):
    marker = isolated_boundaries / "BACKGROUND_HOLD"
    marker.touch()
    resolve = Mock(side_effect=forbidden)
    monkeypatch.setattr(scheduler_provider, "resolve_cron_scheduler", resolve)
    try:
        assert await runner.start()
        marker.unlink()  # Between runner.start and the outer start_gateway cron phase.
        stop, provider, cron, housekeeping = gateway_run._start_gateway_start_cron_and_housekeeping(runner)
        assert (provider, cron, housekeeping) == (None, None, None)
        resolve.assert_not_called()
        assert await gateway_run._await_thread_exit(cron, timeout=0)
        assert await gateway_run._await_thread_exit(housekeeping, timeout=0)
        gateway_run._stop_cron_provider(provider)
        stop.set()
        # A distinct restarted runner may resolve again; stop BEFORE provider effects.
        fresh = SimpleNamespace(config=runner.config, adapters={})
        with pytest.raises(EffectReached):
            gateway_run._start_gateway_start_cron_and_housekeeping(fresh)
    finally:
        await stop_health(runner)


@pytest.mark.asyncio
async def test_actual_shutdown_tail_accepts_absent_held_services(monkeypatch):
    import hermes_cli.nous_auth_keepalive as keepalive
    monkeypatch.setattr(keepalive, "stop_nous_auth_keepalive", Mock())
    monkeypatch.setattr(gateway_run, "_shutdown_mcp_servers_nonblocking", AsyncMock())
    r = SimpleNamespace(should_exit_with_failure=False, exit_code=None, _restart_via_service=False)
    stop, planned = threading.Event(), threading.Event()
    thread = Mock(spec=threading.Thread)
    assert await gateway_run._start_gateway_shutdown_tail(
        r, None, stop, None, None, None, planned, thread, [False]) is True
    assert stop.is_set() and planned.is_set()
    thread.join.assert_called_once_with(timeout=2)


def test_cron_hold_precedes_provider_resolution_availability_and_store(monkeypatch, isolated_boundaries):
    import hermes_cli.config as config
    import plugins.cron_providers as providers
    import cron.jobs as jobs
    marker = isolated_boundaries / "BACKGROUND_HOLD"
    marker.touch()
    available = Mock(return_value=True)
    provider = SimpleNamespace(is_available=available, name="fake-external")
    load = Mock(return_value=provider)
    monkeypatch.setattr(config, "load_config", lambda: {"cron": {"provider": "fake-external"}})
    monkeypatch.setattr(providers, "load_cron_scheduler", load)
    recover = Mock(side_effect=forbidden)
    heartbeat = Mock(side_effect=forbidden)
    monkeypatch.setattr(scheduler_provider.InProcessCronScheduler, "recover_interrupted", recover)
    monkeypatch.setattr(jobs, "record_ticker_heartbeat", heartbeat)
    builtin = scheduler_provider.resolve_cron_scheduler()
    assert isinstance(builtin, scheduler_provider.InProcessCronScheduler)
    load.assert_not_called()
    available.assert_not_called()
    builtin.start(threading.Event())
    recover.assert_not_called()
    heartbeat.assert_not_called()
    marker.unlink()
    assert scheduler_provider.resolve_cron_scheduler() is provider
    load.assert_called_once_with("fake-external")
    available.assert_called_once()
    # Global ESTOP is deliberately NOT a scheduler-creation gate.
    (isolated_boundaries / "ESTOP").touch()
    with pytest.raises(EffectReached):
        builtin.start(threading.Event())
    recover.assert_called_once()
    heartbeat.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_name", ["BACKGROUND_HOLD", "ESTOP"])
async def test_goal_and_heartbeat_stop_before_store_then_resume(runner, isolated_boundaries, marker_name):
    marker = isolated_boundaries / marker_name
    marker.touch()
    args = dict(session_entry=SimpleNamespace(session_id="test-session"), source=event().source,
                final_response="explicit turn finished")
    await runner._post_turn_goal_continuation_scoped(**args)
    await runner._heartbeat_poll_once({"test-session": (event().source, "test-session")})
    runner._warm_goals_session_db.assert_not_called()
    marker.unlink()
    with pytest.raises(EffectReached):
        await runner._post_turn_goal_continuation_scoped(**args)
    with pytest.raises(EffectReached):
        await runner._heartbeat_poll_once({"test-session": (event().source, "test-session")})
    assert runner._warm_goals_session_db.call_count == 2


def expected_callables():
    """Seal references independently of the response; production uses its live bindings."""
    return {
        "hold.is_background_held": estop.is_background_held,
        "hold.check_background_held": estop.check_background_held,
        "startup.start": GatewayStartupMixin.start,
        "cron.gateway_start": gateway_run._start_gateway_start_cron_and_housekeeping,
        "cron.provider_start": scheduler_provider.InProcessCronScheduler.start,
        "goal.post_turn_continuation": GatewayGoalsMixin._post_turn_goal_continuation_scoped,
        "goal.heartbeat_continuation": GatewayGoalsMixin._heartbeat_poll_once,
    }


def accepted(response, expected):
    if response.get("ok") is not True:
        return False
    result = response["result"]
    return (result.get("background_held") is True and result.get("adapter_connected") is True
            and result.get("inbound_startup_gate_ready") is True
            and all(result.get("callables", {}).get(key) == value for key, value in expected.items()))


@pytest.mark.asyncio
@pytest.mark.linux_only
async def test_real_control_socket_loaded_identities_and_fail_closed_runtime(runner, isolated_boundaries, monkeypatch):
    from gateway import control_socket, status
    marker = isolated_boundaries / "BACKGROUND_HOLD"
    marker.touch()
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: isolated_boundaries)
    runner.adapters = {Platform.TELEGRAM: runner.fake_adapter}
    runner._startup_restore_in_progress = False
    expected = {key: runner._fingerprint_loaded_callable(cb) for key, cb in expected_callables().items()}
    server = await gateway_run._start_gateway_start_control_socket(runner)
    assert server is not None

    async def query():
        path = control_socket.resolve_client_socket_path(isolated_boundaries)
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 3)
        try:
            peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            assert struct.unpack("3i", peer)[0] == os.getpid()
            writer.write(b'{"verb":"held-installation-status"}\n')
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 3))
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        response = await query()
        assert response["ok"] is True
        assert response["result"]["adapter_connected"] is False  # Nonempty disconnected map.
        assert response["result"]["connected_adapter_count"] == 0
        assert not accepted(response, expected)
        await runner.fake_adapter.connect()
        response = await query()
        assert accepted(response, expected)
        assert response["result"]["pid"] == os.getpid()
        assert "secret-config-canary" not in json.dumps(response)
        assert set(response["result"]["callables"]["startup.start"]) == {"module", "qualname", "sha256"}
        del runner._startup_restore_in_progress
        assert not accepted(await query(), expected)
        for invalid in (None, 0, "", True):
            runner._startup_restore_in_progress = invalid
            assert not accepted(await query(), expected)
        runner._startup_restore_in_progress = False
        # Loaded instance override must not be hidden by hashing the base class instead.
        runner.start = AsyncMock()
        assert not accepted(await query(), expected)
        del runner.start
        original = estop.check_background_held
        monkeypatch.setattr(estop, "check_background_held", lambda *a: False)
        assert not accepted(await query(), expected)
        monkeypatch.setattr(estop, "check_background_held", None)
        assert not accepted(await query(), expected)
        monkeypatch.setattr(estop, "check_background_held", original)
        assert accepted(await query(), expected)
        for spy in runner.effects.values():
            spy.assert_not_called()
    finally:
        await server.stop()
        atexit.unregister(server.cleanup_files)


def test_loaded_fingerprint_ignores_locations_but_detects_changed_code():
    import types
    def callback():
        return "one"
    fp = GatewayStartupMixin._fingerprint_loaded_callable
    relocated = types.FunctionType(callback.__code__.replace(co_filename="elsewhere.py", co_firstlineno=999), globals())
    relocated.__qualname__ = callback.__qualname__
    assert fp(relocated) == fp(callback)
    altered = types.FunctionType(callback.__code__.replace(co_consts=(None, "two")), globals())
    altered.__qualname__ = callback.__qualname__
    assert fp(altered)["sha256"] != fp(callback)["sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["held", "release-during-connect", "hold-during-connect",
                                    "spool-during-connect", "hold-at-db-open", "hold-during-spool-read", "unheld"])
async def test_outer_startup_preserves_pending_recovery_while_held(
        runner, isolated_boundaries, monkeypatch, timing):
    """The outer entrypoint must preserve a real spool and SQLite messages while held."""
    from gateway import shutdown_flush
    from hermes_state_registry import acquire, release

    home = isolated_boundaries
    marker = home / "BACKGROUND_HOLD"
    if timing not in {"unheld", "hold-during-connect", "hold-at-db-open", "hold-during-spool-read"}:
        marker.touch()
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
    db = acquire(home / "state.db")
    db.create_session(session_id="fixture-session", source="test")
    spool = home / "pending_messages"

    def write_spool():
        shutdown_flush.flush_pending_to_file({
            "fixture-route": {"text": "recover me", "session_id": "fixture-session"},
        })
        spool.chmod(0o750)  # Recovery must not even normalize directory permissions while held.

    def spool_state():
        return (spool.stat().st_mode, spool.stat().st_mtime_ns,
                {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                 for p in spool.iterdir()})

    before = None
    if timing != "spool-during-connect":
        write_spool()
        before = spool_state()

    connect = runner._connect_initial_adapter_with_timeout

    async def connecting(adapter, platform):
        nonlocal before
        connected = await connect(adapter, platform)
        if timing == "release-during-connect":
            marker.unlink()
        elif timing == "hold-during-connect":
            marker.touch()
        elif timing == "spool-during-connect":
            write_spool()
            before = spool_state()
        return connected

    runner._connect_initial_adapter_with_timeout = connecting
    if timing in {"unheld", "hold-during-connect", "hold-at-db-open", "hold-during-spool-read"}:
        runner._start_recover_previous_run = AsyncMock()
        runner._start_startup_warmup = Mock()

        async def finish_wiring(_):
            await runner._finish_startup_restore()

        runner._start_finish_wiring = finish_wiring
        runner._start_spawn_background_watchers = Mock()
        if timing == "hold-during-connect":
            runner._start_finish_wiring = AsyncMock(side_effect=forbidden)
            runner._start_spawn_background_watchers = Mock(side_effect=forbidden)
    if timing == "unheld":
        monkeypatch.setattr(gateway_run, "_start_gateway_start_cron_and_housekeeping",
                            lambda _: (threading.Event(), None, None, None))

    if timing == "hold-at-db-open":
        def acquire_with_hold(*args, **kwargs):
            nonlocal before
            result = acquire(*args, **kwargs)
            marker.touch()
            before = spool_state()
            return result

        monkeypatch.setattr("hermes_state_registry.acquire", acquire_with_hold)

    if timing == "hold-during-spool-read":
        from pathlib import Path
        read_text = Path.read_text
        engaged = False

        def read_with_hold(path, *args, **kwargs):
            nonlocal before, engaged
            text = read_text(path, *args, **kwargs)
            if path.parent == spool and not engaged:
                engaged = True
                marker.touch()
                before = spool_state()
            return text

        monkeypatch.setattr(Path, "read_text", read_with_hold)

    # External lifecycle effects are replaced; start_gateway, runner.start and recovery are real.
    monkeypatch.setattr(gateway_run, "GatewayRunner", lambda config: runner)
    monkeypatch.setattr("hermes_cli.resource_limits.apply_nofile_soft_limit", lambda: None)
    monkeypatch.setattr("gateway.code_skew.record_boot_fingerprint", lambda: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(gateway_run, "_start_gateway_configure_logging", lambda _: None)
    monkeypatch.setattr(gateway_run, "_enable_multiplex_log_routing", lambda _: None)
    monkeypatch.setattr(gateway_run, "_run_planned_stop_watcher", lambda *a: None)
    monkeypatch.setattr(gateway_run, "_start_gateway_claim_pid_file", lambda: True)
    monkeypatch.setattr(gateway_run, "_start_gateway_start_control_socket", AsyncMock())
    monkeypatch.setattr("gateway.lifecycle_ledger.record_startup", lambda: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr(gateway_run, "_shutdown_gateway_health_export", lambda _: None)
    monkeypatch.setattr(gateway_run, "_start_gateway_shutdown_tail", AsyncMock(return_value=True))
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a: None)
    runner.should_exit_cleanly = False
    runner.exit_code = None
    runner._start_systemd_watchdog = lambda: None

    async def available_chat():
        admitted = await runner.fake_adapter.handle_message(event())
        assert admitted is not None
        assert runner._hm_estop_gate(admitted[0], admitted[1], False) is None

    runner.wait_for_shutdown = available_chat
    try:
        assert await gateway_run.start_gateway() is True
        if timing == "unheld":
            assert [m["content"] for m in db.get_messages("fixture-session")] == ["recover me"]
            assert list(spool.iterdir()) == []
        else:
            assert (db.get_messages("fixture-session"), spool_state()) == ([], before)
            # Explicit fixture-only operator recovery after release; marker removal alone did not
            # authorize the outer startup path to replay anything in the latched held boot.
            marker.unlink(missing_ok=True)
            assert shutdown_flush.recover_pending_to_db(db) == 1
            assert [m["content"] for m in db.get_messages("fixture-session")] == ["recover me"]
            assert list(spool.iterdir()) == []
            assert shutdown_flush.recover_pending_to_db(db) == 0
            await available_chat()
    finally:
        await stop_health(runner)
        release(db)
