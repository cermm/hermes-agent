"""Generation changes must order against a goal save already entering SQLite."""
import asyncio
import threading

import pytest

from hermes_cli import goals
from tests.gateway.test_goal_lifecycle_ownership import hermes_home, _make_runner_with_adapter


@pytest.mark.asyncio
async def test_new_generation_fences_a_delayed_durable_goal_save(hermes_home, monkeypatch):
    runner, adapter, entry, source = _make_runner_with_adapter()
    manager = goals.GoalManager(entry.session_id)
    manager.set('original goal')
    original_state = goals.load_goal(entry.session_id)
    db = goals._get_session_db()
    original_save = db.compare_and_set_meta
    entered, release = threading.Event(), threading.Event()
    advancing, published = threading.Event(), threading.Event()
    order = []
    def delayed_save(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        saved = original_save(*args, **kwargs)
        if saved:
            order.append('save')
        return saved
    def advance_generation():
        advancing.set()
        runner._begin_session_run_generation(entry.session_key)
        order.append('generation')
        published.set()
    monkeypatch.setattr(db, 'compare_and_set_meta', delayed_save)
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: ('done', 'old answer', False, None, False))
    task = asyncio.create_task(runner._post_turn_goal_continuation(
        session_entry=entry, source=source, final_response='old response'))
    advance_task = None
    try:
        for _ in range(300):
            if entered.is_set():
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert entered.is_set()
        # This is the native generation claim used when a fresh turn begins.
        advance_task = asyncio.create_task(asyncio.to_thread(advance_generation))
        assert await asyncio.to_thread(advancing.wait, 3)
        # A serialized generation may wait for the earlier writer to commit;
        # publishing first requires that old writer to abandon its mutation.
        published_while_write_pending = await asyncio.to_thread(published.wait, 3)
        release.set()
        await asyncio.wait_for(task, timeout=5)
        await asyncio.wait_for(advance_task, timeout=5)
        assert order != ['generation', 'save'], 'old goal committed after new generation publication'
        if published_while_write_pending:
            assert goals.load_goal(entry.session_id) == original_state
            assert adapter.sends == []
        assert runner._active_goal_run_count() == 0
    finally:
        release.set()
        runner._cancel_all_goal_runs()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if advance_task is not None:
            await asyncio.wait_for(advance_task, timeout=5)
        runner._get_executor().shutdown(wait=True, cancel_futures=True)
