"""Late real GoalManager evaluation must respect newer user-owned goal state."""
import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import MessageEvent
from hermes_cli import goals
from tests.gateway.test_goal_lifecycle_ownership import hermes_home, _make_runner_with_adapter


@pytest.mark.asyncio
async def test_new_generation_does_not_retire_a_notice_already_sending(hermes_home):
    from gateway.platforms.base import BasePlatformAdapter
    runner, adapter, entry, source = _make_runner_with_adapter()
    adapter._post_delivery_callbacks = {}
    adapter.register_post_delivery_callback = BasePlatformAdapter.register_post_delivery_callback.__get__(adapter)
    adapter.pop_post_delivery_callback = BasePlatformAdapter.pop_post_delivery_callback.__get__(adapter)
    generation = runner._begin_session_run_generation(entry.session_key)
    control = runner._begin_goal_run_control(entry.session_key, generation)
    control['executor_done'].set()
    started, release = asyncio.Event(), asyncio.Event()
    async def send(*args, **kwargs):
        started.set()
        await release.wait()
    adapter.send = send
    await runner._defer_goal_status_notice_after_delivery(
        source, 'notice', session_key=entry.session_key, run_generation=generation, control=control)
    callback = adapter.pop_post_delivery_callback(entry.session_key, generation=generation)
    task = asyncio.create_task(callback())
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        newer = runner._begin_session_run_generation(entry.session_key)
        adapter.register_post_delivery_callback(entry.session_key, lambda: None, generation=newer)
        runner._cancel_goal_run(entry.session_key)
        assert runner._active_goal_run_count() == 1
        release.set()
        await asyncio.wait_for(task, timeout=3)
        assert runner._active_goal_run_count() == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_generation_callback_cannot_strand_finished_goal_control(hermes_home):
    from gateway.platforms.base import BasePlatformAdapter
    runner, adapter, entry, source = _make_runner_with_adapter()
    adapter._post_delivery_callbacks = {}
    adapter.register_post_delivery_callback = BasePlatformAdapter.register_post_delivery_callback.__get__(adapter)
    adapter.pop_post_delivery_callback = BasePlatformAdapter.pop_post_delivery_callback.__get__(adapter)
    first_generation = runner._begin_session_run_generation(entry.session_key)
    control = runner._begin_goal_run_control(entry.session_key, first_generation)
    control['executor_done'].set()
    deferred = await runner._defer_goal_status_notice_after_delivery(
        source, 'old goal notice', session_key=entry.session_key,
        run_generation=first_generation, control=control)
    assert deferred and runner._active_goal_run_count() == 1
    next_generation = runner._begin_session_run_generation(entry.session_key)
    foreign = []
    adapter.register_post_delivery_callback(entry.session_key, lambda: foreign.append('new callback'), generation=next_generation)
    runner._cancel_goal_run(entry.session_key)
    assert runner._active_goal_run_count() == 0, 'new callback discarded the only path that retires old goal control'
    callback = adapter.pop_post_delivery_callback(entry.session_key, generation=next_generation)
    assert callable(callback)
    result = callback()
    if asyncio.iscoroutine(result):
        await result
    assert foreign == ['new callback'] and adapter.sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['pause', 'clear', 'replace_after_cancel', 'replace_without_cancel', 'cancel_only'])
@pytest.mark.parametrize('verdict', ['done', 'wait'])
async def test_late_judge_cannot_overwrite_newer_goal_authority(hermes_home, monkeypatch, action, verdict):
    runner, adapter, entry, source = _make_runner_with_adapter()
    manager = goals.GoalManager(entry.session_id)
    manager.set('original goal')
    entered, release = threading.Event(), threading.Event()

    def judge(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return (verdict, 'old goal result', False, {'seconds': 60} if verdict == 'wait' else None, False)

    async def get_manager(event):
        return goals.GoalManager(entry.session_id), entry

    monkeypatch.setattr(goals, 'judge_goal', judge)
    runner._get_goal_manager_for_event = get_manager
    task = asyncio.create_task(runner._post_turn_goal_continuation(
        session_entry=entry, source=source, final_response='old response'))
    try:
        for _ in range(300):
            if entered.is_set():
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert entered.is_set()
        if action == 'cancel_only':
            assert runner._cancel_goal_run(entry.session_key)
        elif action == 'replace_without_cancel':
            goals.GoalManager(entry.session_id).set('replacement goal')
        elif action == 'replace_after_cancel':
            assert runner._cancel_goal_run(entry.session_key)
            goals.GoalManager(entry.session_id).set('replacement goal')
        else:
            await runner._handle_goal_command(MessageEvent(text='/goal ' + action, source=source))
        before = goals.load_goal(entry.session_id)
        release.set()
        await asyncio.wait_for(task, timeout=5)
        after = goals.load_goal(entry.session_id)
        assert after == before, 'retired evaluator overwrote a later user goal mutation'
        assert adapter.sends == [], 'retired evaluation emitted a stale completion notice'
        assert runner._active_goal_run_count() == 0
    finally:
        release.set()
        runner._cancel_all_goal_runs()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runner._get_executor().shutdown(wait=True, cancel_futures=True)
