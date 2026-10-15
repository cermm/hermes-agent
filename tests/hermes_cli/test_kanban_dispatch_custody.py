"""An entered dispatch keeps its native claim until effects are reconciled."""
from pathlib import Path
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_identity import board_identity


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda name: True)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    kb.init_db()
    with kbc.connect_closing() as connection:
        yield connection


@pytest.mark.parametrize("damage", ["missing_pid", "retired_pid", "orphan", "lost_metadata"])
def test_entered_callback_failure_cannot_be_reclaimed_or_spawned_again(board, damage):
    target = kb.create_task(board, title="Protected engineering task", assignee="maker")
    generation = board.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (target,)).fetchone()[0]
    calls = []

    def uncertain(task, workspace, board=None):
        calls.append((task.id, task.current_run_id))
        raise RuntimeError("callback entered; effect retirement unknown")

    result = dispatch.dispatch_once(board, board="default", task_id=target, lane="ready",
        expected_task_event_id=generation, expected_board_identity=board_identity(board),
        expected_assignee="maker", spawn_fn=uncertain, max_spawn=1, max_in_progress=1)
    assert result.effect_unknown == "RuntimeError"
    task = kb.get_task(board, target)
    assert task.status == "running"
    assert task.current_run_id == calls[0][1]
    assert task.claim_lock
    original_lock = task.claim_lock
    expired = int(time.time()) - 7200
    board.execute("UPDATE tasks SET claim_expires=?, last_heartbeat_at=?, started_at=? WHERE id=?",
                  (expired, expired, expired, target))
    board.execute("UPDATE task_runs SET claim_expires=?, started_at=? WHERE id=?",
                  (expired, expired, task.current_run_id))
    if damage == "retired_pid":
        retired = subprocess.Popen([sys.executable, "-c", "pass"])
        assert retired.wait(timeout=5) == 0
        board.execute("UPDATE tasks SET worker_pid=?, max_runtime_seconds=1 WHERE id=?", (retired.pid, target))
        board.execute("UPDATE task_runs SET worker_pid=? WHERE id=?", (retired.pid, task.current_run_id))
    elif damage == "orphan":
        board.execute("UPDATE tasks SET claim_lock=NULL, claim_expires=NULL WHERE id=?", (target,))
        original_lock = None
    elif damage == "lost_metadata":
        board.execute("UPDATE task_runs SET metadata=NULL WHERE id=?", (task.current_run_id,))
    board.commit()

    def no_signal(*args):
        raise AssertionError("unreconciled dispatch must not be signalled by a reclaimer")

    assert kb.release_stale_claims(board, signal_fn=no_signal) == 0
    assert dispatch.detect_stale_running(board, stale_timeout_seconds=1, signal_fn=no_signal) == []
    assert kb.reclaim_task(board, target, reason="try ordinary operator reclaim", signal_fn=no_signal) is False
    assert dispatch.reconcile_orphaned_running(board) == []
    assert dispatch.detect_crashed_workers(board) == []
    assert dispatch.enforce_max_runtime(board, signal_fn=no_signal) == []
    second = dispatch.dispatch_once(board, board="default", spawn_fn=uncertain,
                                    max_spawn=1, max_in_progress=1, stale_timeout_seconds=1)
    assert second.spawned == []
    assert len(calls) == 1
    current = kb.get_task(board, target)
    assert (current.status, current.current_run_id, current.claim_lock) == (
        "running", task.current_run_id, original_lock)
    assert board.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (target,)).fetchone()[0] == 1
