"""Terminal writes preserve the launch-bound task intent for W&C review gates."""
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "boards"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing(board="default") as connection:
        yield connection


@pytest.mark.parametrize("terminal", ["complete", "block"])
@pytest.mark.parametrize("change", ["body", "assignee", "unchanged"])
def test_terminal_transition_matches_original_intent_inside_write_transaction(board, terminal, change):
    task_id = kb.create_task(board, title="A task", body="Original review condition", assignee="reviewer")
    dependent = kb.create_task(board, title="Next gate", assignee="assurance", parents=[task_id])
    claim = kb.claim_task(board, task_id, claimer="private test")
    assert claim is not None
    run_id = claim.current_run_id
    board.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid(), task_id))
    board.execute("UPDATE task_runs SET worker_pid=? WHERE id=?", (os.getpid(), run_id))
    board.commit()
    original = {"body": "Original review condition", "assignee": "reviewer"}
    if change != "unchanged":
        with kbc.connect_closing(board="default") as other:
            if change == "body":
                other.execute("UPDATE tasks SET body='Changed acceptance' WHERE id=?", (task_id,))
            else:
                other.execute("UPDATE tasks SET assignee='builder' WHERE id=?", (task_id,))
            other.commit()
    fence = {"expected_run_id": run_id, "expected_worker_pid": os.getpid(),
             "expected_task_intent": original}
    if terminal == "complete":
        changed = kb.complete_task(board, task_id, summary="Verified result", fire_lifecycle_hook=False, **fence)
    else:
        changed = kb.block_task(board, task_id, kind="needs_input", reason="RETURN: failed oracle", **fence)
    assert changed is (change == "unchanged")
    task = kb.get_task(board, task_id)
    expected = "running" if change != "unchanged" else "done" if terminal == "complete" else "blocked"
    assert task.status == expected
    if change != "unchanged":
        assert task.current_run_id == run_id
    assert kb.get_task(board, dependent).status == ("ready" if expected == "done" else "todo")
