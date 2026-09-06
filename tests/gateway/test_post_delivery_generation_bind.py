"""Retain fork generation snapshot regression against the current adapter owner."""
import pytest
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import build_session_key
from tests.gateway.test_status_command import _make_source

@pytest.mark.asyncio
async def test_post_delivery_callback_generation_snapshot_happens_after_bind():
    """Regression: the callback_generation snapshot in _process_message_background
    must happen AFTER the handler runs, not before.

    _hermes_run_generation is set on the interrupt event by
    GatewayRunner._bind_adapter_run_generation during _handle_message_with_agent.
    The earlier snapshot-at-task-start always captured None, which bypassed the
    generation-ownership check in pop_post_delivery_callback and let stale runs
    fire a fresher run's callbacks.
    """
    import asyncio
    from gateway.platforms.base import BasePlatformAdapter

    source = _make_source()
    session_key = build_session_key(source)
    fired = []

    class _ConcreteAdapter(BasePlatformAdapter):
        platform = Platform.TELEGRAM

        async def connect(self, *, is_reconnect: bool = False): pass
        async def disconnect(self): pass
        async def send(self, chat_id, content, **kwargs): pass
        async def get_chat_info(self, chat_id): return {}

    adapter = _ConcreteAdapter(
        PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM
    )

    async def fake_handler(event):
        # Simulate what _bind_adapter_run_generation does mid-run.
        interrupt_event = adapter._active_sessions.get(session_key)
        setattr(interrupt_event, "_hermes_run_generation", 1)
        # Stale run registers its callback at generation=1.
        adapter.register_post_delivery_callback(
            session_key,
            lambda: fired.append("older"),
            generation=1,
        )
        # A fresher run overwrites with generation=2 (different dict entry).
        adapter.register_post_delivery_callback(
            session_key,
            lambda: fired.append("newer"),
            generation=2,
        )
        return None

    adapter.set_message_handler(fake_handler)
    event = MessageEvent(text="hello", source=source, message_id="m1")

    await adapter.handle_message(event)
    tasks = list(adapter._background_tasks)
    assert tasks, "expected background task to be created"
    await asyncio.gather(*tasks)

    # The stale run (generation=1) must NOT fire the fresher run's callback
    # (generation=2). With the pre-fix code, callback_generation was snapshotted
    # as None before the handler ran, bypassing the ownership check and firing
    # "newer" anyway.
    assert fired == []
    assert session_key in adapter._post_delivery_callbacks
    callback_entry = adapter._post_delivery_callbacks[session_key][0]
    assert callback_entry["generation"] == 2
    assert callable(callback_entry["callback"])
