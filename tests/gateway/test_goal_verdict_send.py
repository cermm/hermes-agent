"""Tests for gateway /goal verdict-message delivery.

The judge verdict message ("✓ Goal achieved", "⏸ budget exhausted", etc.)
must reach the user after each turn. Before this fix the code checked
``hasattr(adapter, "send_message")`` — but adapters expose ``send()``,
never ``send_message``, so the check always evaluated False and users
never saw verdicts. This test locks in the fix.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


class _RecordingAdapter:
    """Minimal adapter that records send() invocations."""

    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self.sends: list[dict] = []

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        self.sends.append({"chat_id": chat_id, "content": content, "metadata": metadata})

        class _R:
            success = True
            message_id = "mock-msg"

        return _R()


def _make_runner_with_adapter(session_id: str = None):
    from gateway.run import GatewayRunner
    import uuid

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._queued_events = {}

    src = _make_source()
    # Default to a unique session_id so xdist parallel runs on the same worker
    # don't see each other's GoalManager state (DEFAULT_DB_PATH gets frozen at
    # module-import time, defeating per-test HERMES_HOME monkeypatches).
    session_entry = SessionEntry(
        session_key=build_session_key(src),
        session_id=session_id or f"goal-sess-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._generate_session_key.return_value = build_session_key(src)

    adapter = _RecordingAdapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter, session_entry, src


@pytest.mark.asyncio
async def test_goal_verdict_done_sent_via_adapter_send(hermes_home):
    """When the judge says done, the '✓ Goal achieved' message must reach
    the user through the adapter's ``send()`` method."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_entry.session_id)
    mgr.set("ship the feature")

    with patch("hermes_cli.goals.judge_goal", return_value=("done", "the feature shipped", False, None)):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="I shipped the feature.",
        )
        # fire-and-forget create_task — give the loop a tick
        await asyncio.sleep(0.05)

    assert len(adapter.sends) == 1, f"expected 1 send, got {len(adapter.sends)}: {adapter.sends}"
    msg = adapter.sends[0]
    assert msg["chat_id"] == "c1"
    assert "Goal achieved" in msg["content"]
    assert "the feature shipped" in msg["content"]


@pytest.mark.asyncio
async def test_goal_verdict_continue_enqueues_continuation(hermes_home):
    """When the judge says continue, both the 'continuing' status and the
    continuation-prompt event must be delivered. The continuation prompt is
    routed through the adapter's pending-messages FIFO so the goal loop
    proceeds on the next turn."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_entry.session_id)
    mgr.set("polish the docs")

    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "still needs work", False, None)):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="here's a partial edit",
        )
        await asyncio.sleep(0.05)

    # Status line sent back
    assert len(adapter.sends) == 1
    assert "Continuing toward goal" in adapter.sends[0]["content"]
    # Continuation prompt enqueued for next turn
    assert adapter._pending_messages, "continuation prompt must be enqueued in pending_messages"


@pytest.mark.asyncio
async def test_goal_verdict_budget_exhausted_sends_pause(hermes_home):
    """When the budget is exhausted, a '⏸ Goal paused' message must be sent
    and no further continuation enqueued."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager, save_goal

    mgr = GoalManager(session_entry.session_id, default_max_turns=2)
    state = mgr.set("tiny goal", max_turns=2)
    state.turns_used = 2
    save_goal(session_entry.session_id, state)

    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "keep going", False, None)):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="still partial",
        )
        await asyncio.sleep(0.05)

    assert len(adapter.sends) == 1
    content = adapter.sends[0]["content"]
    assert "paused" in content.lower()
    assert "turns used" in content.lower()
    # No continuation enqueued when budget is exhausted
    assert not adapter._pending_messages


@pytest.mark.asyncio
async def test_goal_verdict_skipped_when_no_active_goal(hermes_home):
    """No goal set → the hook is a no-op. Nothing is sent, nothing enqueued."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    await runner._post_turn_goal_continuation(
        session_entry=session_entry,
        source=src,
        final_response="anything",
    )
    await asyncio.sleep(0.05)

    assert adapter.sends == []
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_goal_verdict_survives_adapter_without_send(hermes_home):
    """Bad adapter (no ``send`` attribute) must not crash the judge hook."""
    runner, _adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager

    GoalManager(session_entry.session_id).set("survive missing send")

    class _NoSendAdapter:
        def __init__(self):
            self._pending_messages: dict = {}

    runner.adapters[Platform.TELEGRAM] = _NoSendAdapter()

    with patch("hermes_cli.goals.judge_goal", return_value=("done", "ok", False, None)):
        # must not raise
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="whatever",
        )
        await asyncio.sleep(0.05)

@pytest.mark.asyncio
async def test_replaced_goal_generation_remains_in_shutdown_drain_until_done(
    hermes_home,
):
    runner, _adapter, session_entry, _src = _make_runner_with_adapter()
    session_key = session_entry.session_key

    predecessor = runner._begin_goal_run_control(session_key, 1)
    replacement = runner._begin_goal_run_control(session_key, 2)

    assert predecessor["cancel"].is_set() is True
    assert predecessor["done"].is_set() is False
    runner._finish_goal_run_control(session_key, replacement)

    assert runner._active_goal_run_count() == 1
    active_agents, timed_out = await runner._drain_active_agents(0.01)
    assert active_agents == {}
    assert timed_out is True

    runner._finish_goal_run_control(session_key, predecessor)
    assert runner._active_goal_run_count() == 0


@pytest.mark.asyncio
async def test_goal_control_remains_owned_through_post_evaluation_notice(
    hermes_home, monkeypatch
):
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager

    GoalManager(session_entry.session_id).set("continue after the status notice")
    notice_started = asyncio.Event()
    release_notice = asyncio.Event()

    async def _blocked_notice(_source, _message, **_kwargs):
        notice_started.set()
        await release_notice.wait()

    monkeypatch.setattr(
        runner, "_defer_goal_status_notice_after_delivery", _blocked_notice
    )
    task = None
    try:
        with patch(
            "hermes_cli.goals.judge_goal",
            return_value=("continue", "needs another turn", False, None),
        ):
            task = asyncio.create_task(
                runner._post_turn_goal_continuation(
                    session_entry=session_entry,
                    source=src,
                    final_response="partial result",
                )
            )
            await asyncio.wait_for(notice_started.wait(), timeout=1.0)
            assert runner._active_goal_run_count() == 1

            runner._cancel_all_goal_runs()
            release_notice.set()
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        release_notice.set()
        runner._cancel_all_goal_runs()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert runner._active_goal_run_count() == 0
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_shutdown_removes_generation_owned_post_delivery_goal_notice(
    hermes_home,
):
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from gateway.platforms.base import BasePlatformAdapter
    from hermes_cli.goals import GoalManager

    adapter._post_delivery_callbacks = {}
    adapter.register_post_delivery_callback = (
        BasePlatformAdapter.register_post_delivery_callback.__get__(adapter)
    )
    adapter.pop_post_delivery_callback = (
        BasePlatformAdapter.pop_post_delivery_callback.__get__(adapter)
    )
    GoalManager(session_entry.session_id).set("finish without a stale notice")

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("done", "complete", False, None),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="finished",
        )

    session_key = session_entry.session_key
    assert runner._active_goal_run_count() == 1
    assert session_key in adapter._post_delivery_callbacks

    runner._cancel_all_goal_runs()

    assert runner._active_goal_run_count() == 0
    assert adapter.pop_post_delivery_callback(session_key, generation=0) is None
    assert adapter.sends == []


@pytest.mark.asyncio
async def test_post_delivery_goal_notice_retires_control_only_after_callback(
    hermes_home,
):
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from gateway.platforms.base import BasePlatformAdapter
    from hermes_cli.goals import GoalManager

    adapter._post_delivery_callbacks = {}
    adapter.register_post_delivery_callback = (
        BasePlatformAdapter.register_post_delivery_callback.__get__(adapter)
    )
    adapter.pop_post_delivery_callback = (
        BasePlatformAdapter.pop_post_delivery_callback.__get__(adapter)
    )
    GoalManager(session_entry.session_id).set("deliver before retiring ownership")

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("done", "complete", False, None),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="finished",
        )

    callback = adapter.pop_post_delivery_callback(
        session_entry.session_key,
        generation=0,
    )
    assert callable(callback)
    assert runner._active_goal_run_count() == 1

    await callback()

    assert runner._active_goal_run_count() == 0
    assert len(adapter.sends) == 1
    assert "Goal achieved" in adapter.sends[0]["content"]


@pytest.mark.asyncio
async def test_cancelled_goal_notice_preserves_foreign_post_delivery_callback(
    hermes_home,
):
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from gateway.platforms.base import BasePlatformAdapter
    from hermes_cli.goals import GoalManager

    adapter._post_delivery_callbacks = {}
    adapter.register_post_delivery_callback = (
        BasePlatformAdapter.register_post_delivery_callback.__get__(adapter)
    )
    adapter.pop_post_delivery_callback = (
        BasePlatformAdapter.pop_post_delivery_callback.__get__(adapter)
    )
    session_key = session_entry.session_key
    foreign_deliveries = []

    def _foreign_callback():
        foreign_deliveries.append("foreign-delivered")

    adapter.register_post_delivery_callback(
        session_key,
        _foreign_callback,
        generation=0,
    )
    GoalManager(session_entry.session_id).set("finish without cancelling unrelated callbacks")

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("done", "complete", False, None),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="finished",
        )

    assert runner._active_goal_run_count() == 1

    runner._cancel_all_goal_runs()
    callback = adapter.pop_post_delivery_callback(session_key, generation=0)
    assert callable(callback)
    result = callback()
    if asyncio.iscoroutine(result):
        await result

    assert foreign_deliveries == ["foreign-delivered"]
    assert adapter.sends == []
    assert runner._active_goal_run_count() == 0


@pytest.mark.asyncio
async def test_post_evaluation_cancellation_does_not_interrupt_reused_worker(
    hermes_home, monkeypatch
):
    runner, _adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager
    from tools.interrupt import clear_current_thread_interrupt, is_interrupted

    GoalManager(session_entry.session_id).set("retain lifecycle without thread ownership")
    runner._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    notice_started = asyncio.Event()
    release_notice = asyncio.Event()
    goal_worker_threads = []

    async def _blocked_notice(_source, _message, **_kwargs):
        notice_started.set()
        await release_notice.wait()

    def _observe_reused_worker():
        observed = (threading.get_ident(), is_interrupted())
        clear_current_thread_interrupt()
        return observed

    def _judge_goal(*_args, **_kwargs):
        goal_worker_threads.append(threading.get_ident())
        return ("continue", "needs another turn", False, None)

    monkeypatch.setattr(
        runner, "_defer_goal_status_notice_after_delivery", _blocked_notice
    )
    task = None
    try:
        with patch("hermes_cli.goals.judge_goal", side_effect=_judge_goal):
            task = asyncio.create_task(
                runner._post_turn_goal_continuation(
                    session_entry=session_entry,
                    source=src,
                    final_response="partial result",
                )
            )
            await asyncio.wait_for(notice_started.wait(), timeout=1.0)
            control = next(iter(runner._goal_run_controls[session_entry.session_key]))
            assert goal_worker_threads
            completed_thread_id = goal_worker_threads[0]
            assert control["executor_done"].is_set() is True
            assert control["thread_id"] is None

            runner._cancel_all_goal_runs()
            reused_thread_id, observed_interrupt = (
                await runner._run_in_executor_with_context(_observe_reused_worker)
            )
            assert reused_thread_id == completed_thread_id
            assert observed_interrupt is False
    finally:
        release_notice.set()
        runner._cancel_all_goal_runs()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runner._executor.shutdown(wait=True, cancel_futures=True)
