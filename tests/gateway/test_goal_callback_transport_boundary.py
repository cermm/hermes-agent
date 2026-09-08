"""Goal revocation across the real adapter background delivery boundary."""
import asyncio
from unittest.mock import patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from tests.gateway.test_post_delivery_callback_chaining import _MinAdapter
from tests.gateway.test_goal_lifecycle_ownership import hermes_home, _make_runner_with_adapter


class _PausedTransport(_MinAdapter):
    def __init__(self, pause_notice=False):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.pause_notice = pause_notice
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.started = []
        self.completed = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.started.append(content)
        if ('Goal achieved' in content) == self.pause_notice:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.release.wait()
        self.completed.append(content)
        return SendResult(success=True, message_id=str(len(self.completed)))


async def _start_delivery(runner, adapter, entry, source):
    from hermes_cli.goals import GoalManager

    runner.adapters[Platform.TELEGRAM] = adapter
    generation = runner._begin_session_run_generation(entry.session_key)
    interrupt = asyncio.Event()
    interrupt._hermes_run_generation = generation
    adapter._active_sessions[entry.session_key] = interrupt
    GoalManager(entry.session_id).set('verify the actual delivery boundary')

    async def handler(event):
        with patch('hermes_cli.goals.judge_goal', return_value=('done', 'complete', False, None, False)):
            await runner._post_turn_goal_continuation(
                session_entry=entry, source=source, final_response='candidate ready',
                run_generation=generation)
        return 'candidate ready'

    adapter._message_handler = handler
    task = asyncio.create_task(adapter._process_message_background(
        MessageEvent(text='finish objective', source=source), entry.session_key))
    adapter._track_session_task(entry.session_key, task)
    return task, generation


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke', ['cancel', 'new_generation'])
async def test_revocation_during_main_delivery_prevents_deferred_goal_send(hermes_home, revoke):
    runner, _, entry, source = _make_runner_with_adapter()
    adapter = _PausedTransport()
    task, _ = await _start_delivery(runner, adapter, entry, source)
    try:
        await asyncio.wait_for(adapter.entered.wait(), timeout=5)
        assert runner._active_goal_run_count() == 1
        if revoke == 'cancel':
            runner._cancel_goal_run(entry.session_key)
        else:
            runner._begin_session_run_generation(entry.session_key)
        adapter.release.set()
        await asyncio.wait_for(task, timeout=5)
        assert adapter.started == ['candidate ready']
        assert adapter.completed == ['candidate ready']
        assert runner._active_goal_run_count() == 0
    finally:
        adapter.release.set()
        await asyncio.gather(task, return_exceptions=True)
        runner._cancel_all_goal_runs()
        runner._get_executor().shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("additional_callback", [False, True])
async def test_timed_out_inflight_notice_retains_owner_until_transport_finishes(hermes_home, monkeypatch, additional_callback):
    """Cancellation cannot retract a send already entered; another callback still progresses."""
    from gateway.platforms import base

    monkeypatch.setattr(base, '_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS', 0.1)
    runner, _, entry, source = _make_runner_with_adapter()
    adapter = _PausedTransport(pause_notice=True)
    independent = asyncio.Event()
    # Register after generation allocation, before the scheduled handler runs.
    task, generation = await _start_delivery(runner, adapter, entry, source)
    if additional_callback:
        adapter.register_post_delivery_callback(entry.session_key, independent.set, generation=generation)
    try:
        await asyncio.wait_for(adapter.entered.wait(), timeout=5)
        if additional_callback:
            await asyncio.wait_for(independent.wait(), timeout=5)
        await asyncio.wait_for(adapter.cancel_seen.wait(), timeout=5)
        runner._begin_session_run_generation(entry.session_key)
        runner._cancel_goal_run(entry.session_key)
        assert runner._active_goal_run_count() == 1
        assert len(adapter.started) == 2
        assert adapter.completed == ['candidate ready']
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done, "callback cancellation must not hold the background session indefinitely"
        adapter.release.set()
        await asyncio.wait_for(task, timeout=5)
        for _ in range(100):
            if runner._active_goal_run_count() == 0:
                break
            await asyncio.sleep(0.01)
        assert runner._active_goal_run_count() == 0
        assert len(adapter.started) == 2
        assert adapter.completed == adapter.started
    finally:
        adapter.release.set()
        await asyncio.gather(task, return_exceptions=True)
        runner._cancel_all_goal_runs()
        runner._get_executor().shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_composed_timed_out_callback_is_retained_until_it_really_finishes(hermes_home, monkeypatch):
    import gc
    import weakref
    from gateway.platforms import base

    monkeypatch.setattr(base, '_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS', 0.05)
    adapter = _MinAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    pending_refs = []
    cancelled = asyncio.Event()
    finished = asyncio.Event()

    async def resistant():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pending = asyncio.Future()
            pending_refs.append(weakref.ref(pending))
            cancelled.set()
            await pending
        finished.set()

    adapter.register_post_delivery_callback('retention', resistant)
    adapter.register_post_delivery_callback('retention', lambda: None)
    await adapter._fire_post_delivery_callback('retention', asyncio.Event())
    await asyncio.wait_for(cancelled.wait(), timeout=5)
    try:
        gc.collect()
        assert pending_refs[0]() is not None, 'timed-out composed callback lost its owner before completion'
        pending_refs[0]().set_result(None)
        await asyncio.wait_for(finished.wait(), timeout=5)
        await asyncio.sleep(0)
        assert not adapter._inflight_post_delivery_callbacks
    finally:
        if pending_refs and pending_refs[0]() is not None and not pending_refs[0]().done():
            pending_refs[0]().set_result(None)
