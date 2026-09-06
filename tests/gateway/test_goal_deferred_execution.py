"""Real shell quality gates obey deferred consent and cancellation boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shlex
import sys
import threading
import time
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig
from hermes_cli.goals import GoalGate, run_gate
from tools import approval, approval_context
from tools.interrupt import clear_current_thread_interrupt, set_interrupt
from tests.gateway.test_goal_lifecycle_ownership import hermes_home, _make_runner_with_adapter


def command_for(script):
    return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'


def test_denied_deferred_gate_never_spawns_shell(tmp_path, monkeypatch):
    marker = tmp_path / 'should-not-exist'
    command = command_for(f'from pathlib import Path; Path({str(marker)!r}).write_text("spawned")')
    monkeypatch.setattr(approval, '_presence', lambda cb=None: (cb, True, False, False))
    monkeypatch.setattr(approval, '_tirith_scan', lambda command: {'action': 'allow'})
    monkeypatch.setattr(approval, '_present_with_selected_transport', lambda **kw: None)
    monkeypatch.setattr(approval, '_transport_choice', lambda *a, **kw: (None, None))
    monkeypatch.setattr(approval, 'prompt_dangerous_approval', lambda *a, **kw: 'deny')
    token = approval_context.set_deferred_command_session_authorization_required(True)
    session = approval_context.set_current_session_key('denied-goal-gate')
    try:
        passed, code, output = run_gate(GoalGate(command=command, timeout_seconds=5))
    finally:
        approval_context.reset_current_session_key(session)
        approval_context.reset_deferred_command_session_authorization_required(token)
    assert passed is False
    assert code != 0
    assert not marker.exists()
    assert 'BLOCKED' in output


@pytest.mark.linux_only
def test_running_deferred_gate_observes_thread_interrupt(tmp_path, monkeypatch):
    ready = tmp_path / 'ready'
    command = command_for(f'from pathlib import Path; import time; Path({str(ready)!r}).write_text("ready"); time.sleep(5)')
    monkeypatch.setattr(approval, 'check_all_command_guards', lambda *a, **kw: {'approved': True})
    worker_id = []
    def execute():
        token = approval_context.set_deferred_command_session_authorization_required(True)
        worker_id.append(threading.get_ident())
        try:
            return run_gate(GoalGate(command=command, timeout_seconds=10))
        finally:
            clear_current_thread_interrupt()
            approval_context.reset_deferred_command_session_authorization_required(token)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(execute)
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        set_interrupt(True, thread_id=worker_id[0])
        passed, code, output = future.result(timeout=3)
    assert passed is False and code != 0
    assert 'interrupted' in output


@pytest.mark.asyncio
@pytest.mark.parametrize('message,required', [
    ('ordinary request', False),
    ('[Continuing toward your standing goal]\nGoal: test', True),
    ('[Continuing toward your standing goal — a quality gate failed]\nGoal: test', True),
])
async def test_real_turn_wrapper_scopes_deferred_authority(message, required):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    observed = []
    async def inner(*args, **kwargs):
        observed.append(approval_context.deferred_command_session_authorization_required())
        raise RuntimeError('synthetic end of turn')
    runner._run_agent_inner = inner
    with pytest.raises(RuntimeError, match='synthetic'):
        await runner._run_agent(message, '', [], None, 'test-session')
    assert observed == [required]
    assert approval_context.deferred_command_session_authorization_required() is False


@pytest.mark.asyncio
async def test_cancel_before_executor_start_retires_goal_control(hermes_home):
    runner, adapter, entry, source = _make_runner_with_adapter()
    runner._warm_goals_session_db = AsyncMock()
    runner._executor = ThreadPoolExecutor(max_workers=1)
    blocker_release = threading.Event()
    blocker_entered = threading.Event()
    def blocker():
        blocker_entered.set()
        blocker_release.wait(3)
    runner._executor.submit(blocker)
    assert blocker_entered.wait(3)
    task = asyncio.create_task(runner._post_turn_goal_continuation(
        session_entry=entry, source=source, final_response='completed turn',
    ))
    try:
        for _ in range(300):
            if runner._active_goal_run_count():
                break
            await asyncio.sleep(0.01)
        assert runner._active_goal_run_count() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        blocker_release.set()
        for _ in range(300):
            if runner._active_goal_run_count() == 0:
                break
            await asyncio.sleep(0.01)
        assert runner._active_goal_run_count() == 0
        assert adapter.sends == []
    finally:
        blocker_release.set()
        runner._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_real_gateway_goal_gate_waits_for_owned_approval(hermes_home, tmp_path, monkeypatch, cancel):
    from hermes_cli import goals
    runner, adapter, entry, source = _make_runner_with_adapter()
    marker = tmp_path / 'approved-execution'
    manager = goals.GoalManager(entry.session_id)
    manager.set('execute an explicitly authorized gate')
    manager.add_gate(command_for(f'from pathlib import Path; Path({str(marker)!r}).write_text("approved")'))
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **kw: ('done', 'gate passed', False, None, False))
    monkeypatch.setattr(approval, '_tirith_scan', lambda command: {'action': 'allow'})
    monkeypatch.setattr(approval, '_present_with_selected_transport', lambda **kw: None)
    monkeypatch.setattr(approval, '_transport_choice', lambda *a, **kw: (None, None))
    monkeypatch.setattr(approval_context, '_get_approval_timeout', lambda: 3)
    task = asyncio.create_task(runner._post_turn_goal_continuation(
        session_entry=entry, source=source, final_response='ready for verification',
    ))
    try:
        for _ in range(200):
            pending = approval.list_gateway_approvals(entry.session_key)
            if pending and adapter.sends:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert len(pending) == 1
        assert not marker.exists()
        assert 'approve session' in adapter.sends[0]['content']
        assert adapter.sends[0]['metadata']['_interim_send'] is True
        before = goals.load_goal(entry.session_id)
        if cancel:
            runner._cancel_goal_run(entry.session_key)
        else:
            approval.resolve_gateway_approval(entry.session_key, 'session', request_id=pending[0]['request_id'])
        await asyncio.wait_for(task, timeout=3)
        assert marker.exists() is (not cancel)
        if cancel:
            assert goals.load_goal(entry.session_id) == before
        assert runner._active_goal_run_count() == 0
        assert approval.list_gateway_approvals(entry.session_key) == []
    finally:
        runner._cancel_all_goal_runs()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runner._get_executor().shutdown(wait=True, cancel_futures=True)
