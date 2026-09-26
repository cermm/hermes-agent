"""The held-status socket reports actual registrations without discovering plugins."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

from gateway import run as gateway_run
from gateway.control_socket import GatewayControlServer, resolve_client_socket_path
from gateway.platforms.base import BasePlatformAdapter
from gateway.run_goals import GatewayGoalsMixin
from gateway.run_startup import GatewayStartupMixin
from hermes_cli import plugins

PLUGIN_SOURCE = """def observed(**kwargs):
    raise AssertionError("status must not invoke callbacks")

def secondary(**kwargs):
    raise AssertionError("second callback must not execute")

class Observer:
    def method(self, **kwargs):
        raise AssertionError("bound callback must not execute")

def register(ctx):
    ctx.register_hook("post_tool_call", observed)
    ctx.register_hook("on_session_start", secondary)
"""


@pytest.fixture
def registered_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    plugins._reset_plugin_managers_for_tests()
    empty = tmp_path / "bundled"
    empty.mkdir()
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: empty)
    directory = tmp_path / "plugins" / "status-fixture"
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text("name: status-fixture\nversion: 1.0.0\n")
    (directory / "__init__.py").write_text(PLUGIN_SOURCE)
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: [status-fixture]\n")
    (tmp_path / "BACKGROUND_HOLD").touch()
    manager = plugins.get_plugin_manager()
    manager.discover_and_load()
    loaded = manager._plugins["status-fixture"]
    assert loaded.enabled, loaded.error
    try:
        yield tmp_path, manager, loaded
    finally:
        plugins._reset_plugin_managers_for_tests()


class LocalAdapter:
    is_connected = BasePlatformAdapter.is_connected
    _running = True


class StatusRunner(GatewayStartupMixin, GatewayGoalsMixin):
    pass


async def open_status(home):
    runner = StatusRunner()
    runner.adapters = {"local": LocalAdapter()}
    runner._startup_restore_in_progress = False
    server = GatewayControlServer(home, verb_handlers={"held-installation-status": runner._held_installation_status})
    assert await server.start()
    async def query():
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(resolve_client_socket_path(home))), 3)
        try:
            assert struct.unpack("3i", writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0] > 0
            writer.write(b'{"verb":"held-installation-status"}\n')
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 3))
            assert response["ok"] is True
            return response["result"]
        finally:
            writer.close()
            await writer.wait_closed()
    return server, query


@pytest.mark.asyncio
@pytest.mark.linux_only
async def test_held_status_fingerprints_actual_generic_hook_registrations(registered_plugin):
    home, manager, loaded = registered_plugin
    server, query = await open_status(home)
    try:
        first = await query()
        snapshot = first["plugin_hooks"]
        assert first["background_held"] is True and first["adapter_connected"] is True
        assert snapshot["status"] == "ready" and snapshot["manager_scope"] == str(home)
        observed = snapshot["hooks"]["post_tool_call"][0]
        assert observed["module"] == loaded.module.__name__
        assert observed["qualname"] == "observed" and observed["registered_module_identity"] is True
        context = plugins.PluginContext(loaded.manifest, manager)
        duplicate = context.register_hook("post_tool_call", loaded.module.observed)
        assert (await query())["plugin_hooks"]["hooks"]["post_tool_call"] == [observed, observed]
        duplicate.dispose()
        loaded.module.observed.__func__ = loaded.module.secondary
        try:
            # Plain functions may carry arbitrary attributes; only real bound methods unwrap.
            assert (await query())["plugin_hooks"]["hooks"]["post_tool_call"] == [observed]
        finally:
            del loaded.module.observed.__func__
        bound = context.register_hook("post_tool_call", loaded.module.Observer().method)
        try:
            records = (await query())["plugin_hooks"]["hooks"]["post_tool_call"]
            assert records[0] == observed
            assert records[1]["module"] == loaded.module.__name__
            assert records[1]["qualname"] == "Observer.method"
            assert records[1]["registered_module_identity"] is True
            assert records[1]["sha256"] != observed["sha256"]
        finally:
            bound.dispose()
        original_code = loaded.module.observed.__code__
        loaded.module.observed.__code__ = original_code.replace(co_consts=tuple(
            "changed actual callback" if isinstance(value, str) else value for value in original_code.co_consts))
        try:
            changed = (await query())["plugin_hooks"]["hooks"]["post_tool_call"][0]
            assert changed["sha256"] != observed["sha256"]
        finally:
            loaded.module.observed.__code__ = original_code
        original_function = loaded.module.observed
        loaded.module.observed = loaded.module.secondary
        try:
            # Dispatch still holds the old callable: a fresh module attribute is not the witness.
            assert (await query())["plugin_hooks"]["hooks"]["post_tool_call"] == [observed]
        finally:
            loaded.module.observed = original_function
        handle = next(r for r in manager._ownership_ledger["status-fixture"]
                      if r.kind == "hook" and r.key == "post_tool_call" and r.active)
        handle.dispose()
        assert "post_tool_call" not in (await query())["plugin_hooks"]["hooks"]
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.linux_only
async def test_held_status_snapshot_is_noncreating_bounded_and_profile_scoped(registered_plugin, monkeypatch):
    from hermes_cli.plugins_runtime import MAX_SNAPSHOT_CALLBACKS
    home, manager, loaded = registered_plugin
    server, query = await open_status(home)
    try:
        entered, release = threading.Event(), threading.Event()
        def lock_registration():
            with manager._discovery_lock:
                entered.set()
                release.wait(5)
        thread = threading.Thread(target=lock_registration)
        thread.start()
        try:
            assert entered.wait(2)
            busy = (await query())["plugin_hooks"]
            assert busy["status"] == "unavailable" and busy["reason"] == "registration_busy"
        finally:
            release.set()
            thread.join(2)
            assert not thread.is_alive()
        context = plugins.PluginContext(loaded.manifest, manager)
        opaque = context.register_hook("post_tool_call", object())
        try:
            records = (await query())["plugin_hooks"]["hooks"]["post_tool_call"]
            assert records[-1] == {"supported": False, "reason": "unsupported_callable"}
        finally:
            opaque.dispose()
        excess = [context.register_hook("post_tool_call", loaded.module.observed)
                  for _ in range(MAX_SNAPSHOT_CALLBACKS - 2)]
        try:
            assert (await query())["plugin_hooks"]["status"] == "ready"
            excess.append(context.register_hook("post_tool_call", loaded.module.observed))
            bounded = (await query())["plugin_hooks"]
            assert bounded["status"] == "unavailable" and bounded["reason"] == "snapshot_limit"
            assert bounded["hooks"] == {}
        finally:
            for handle in excess:
                handle.dispose()
        # Pause only observation; a separate thread uses the real public registration API.
        from hermes_cli.plugins_runtime import _callback_identity
        entered, changed = threading.Event(), threading.Event()
        handles = []
        def append_while_observed():
            if entered.wait(2):
                handles.extend(context.register_hook("post_tool_call", loaded.module.observed)
                               for _ in range(MAX_SNAPSHOT_CALLBACKS))
                changed.set()
        def observe(frame, event, arg):
            if event == "call" and frame.f_code is _callback_identity.__code__ and not entered.is_set():
                entered.set()
                assert changed.wait(2)
        thread = threading.Thread(target=append_while_observed)
        loop = asyncio.get_running_loop()
        previous_executor = loop._default_executor
        executor = ThreadPoolExecutor(max_workers=1, initializer=sys.setprofile, initargs=(observe,))
        loop.set_default_executor(executor)
        thread.start()
        try:
            bounded = (await query())["plugin_hooks"]
            assert entered.is_set() and changed.is_set()
            assert bounded["status"] == "unavailable" and bounded["reason"] == "snapshot_limit"
            assert bounded["hooks"] == {}
        finally:
            loop.set_default_executor(previous_executor)
            executor.shutdown(wait=True)
            thread.join(2)
            assert not thread.is_alive()
            for handle in handles:
                handle.dispose()
        other_home = home / "other-profile"
        other_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(other_home))
        before = dict(plugins._plugin_managers_by_home)
        absent = (await query())["plugin_hooks"]
        assert absent["status"] == "unavailable" and absent["reason"] == "manager_missing"
        assert plugins._plugin_managers_by_home == before
        other = plugins.get_plugin_manager()
        # Actual second profile manager exists but has no registration; it never borrows the first.
        empty = (await query())["plugin_hooks"]
        assert empty["status"] == "ready" and empty["manager_scope"] == str(other_home) and empty["hooks"] == {}
        assert other is not manager
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert (await query())["plugin_hooks"]["hooks"]["post_tool_call"][0]["module"] == loaded.module.__name__
    finally:
        await server.stop()
