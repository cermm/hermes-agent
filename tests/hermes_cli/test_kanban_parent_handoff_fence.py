"""A contained child cannot finalize against a changed parent handoff."""

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_identity import board_identity


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    profile = home / "profiles" / "maker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  default: inert\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "boards"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    kb.init_db()
    with kbc.connect_closing(board="default") as conn:
        yield conn


def _launched_child(board):
    parent = kb.create_task(board, title="parent", assignee="maker")
    parent_claim = kb.claim_task(board, parent, claimer="private parent")
    assert parent_claim is not None
    assert kb.complete_task(
        board, parent, summary="candidate-v1", metadata={"artifact": {"sha": "v1"}},
        expected_run_id=parent_claim.current_run_id, fire_lifecycle_hook=False,
    )
    child = kb.create_task(board, title="child", assignee="maker", parents=[parent])
    downstream = kb.create_task(board, title="downstream", assignee="maker", parents=[child])
    generation = board.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id=?", (child,),
    ).fetchone()[0]
    identity = board_identity(board)
    result = kbd.dispatch_once(
        board, spawn_fn=lambda task, workspace, board=None: os.getpid(),
        max_spawn=1, max_in_progress=1, max_in_progress_per_profile=1,
        board="default", task_id=child, lane="ready",
        expected_task_event_id=generation, expected_board_identity=identity,
        expected_assignee="maker",
    )
    assert result.selection_rejected is None and result.effect_unknown is None
    run_id = int(result.spawned_run_ids[child])
    parent_run = board.execute(
        "SELECT id,status,outcome,started_at,ended_at,summary,metadata "
        "FROM task_runs WHERE task_id=? AND outcome='completed' "
        "ORDER BY started_at DESC,id DESC LIMIT 1", (parent,),
    ).fetchone()
    witness = [{"task_id": parent, "run_id": str(parent_run["id"]),
                "status": "done", "ended_at": parent_run["ended_at"],
                "summary": parent_run["summary"],
                "metadata": json.loads(parent_run["metadata"])}]
    assert witness[0]["metadata"]["artifact"]["sha"] == "v1"
    return parent, child, downstream, run_id, identity, witness


@pytest.mark.parametrize("terminal", ["complete", "block"])
@pytest.mark.parametrize("change", ["metadata", "new_completed_run", "add_parent", "remove_parent"])
def test_changed_parent_handoff_refuses_terminal_transition(board, terminal, change):
    parent, child, downstream, run_id, identity, witness = _launched_child(board)
    # A separate connection mutates committed parent input after the child was
    # launched, while the child claim and worker PID remain valid.
    with kbc.connect_closing(board="default") as other:
        if change == "metadata":
            other.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({"artifact": {"sha": "v2"}}), int(witness[0]["run_id"])),
            )
        elif change == "new_completed_run":
            previous = other.execute(
                "SELECT started_at,ended_at FROM task_runs WHERE id=?",
                (int(witness[0]["run_id"]),),
            ).fetchone()
            other.execute(
                "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,summary,metadata) "
                "VALUES (?, 'maker', 'done', ?, ?, 'completed', 'candidate-v2', ?)",
                (parent, previous["started_at"] + 1, previous["ended_at"] + 1,
                 json.dumps({"artifact": {"sha": "v2"}})),
            )
        elif change == "add_parent":
            extra = kb.create_task(other, title="extra parent", assignee="maker")
            assert kb.complete_task(other, extra, summary="extra candidate", fire_lifecycle_hook=False)
            other.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (extra, child))
        else:
            other.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (parent, child))
        other.commit()

    fence = dict(expected_run_id=run_id, expected_worker_pid=os.getpid(),
                 expected_board_identity=identity, expected_parent_handoffs=witness)
    if terminal == "complete":
        changed = kb.complete_task(
            board, child, summary="verified child output", fire_lifecycle_hook=False, **fence,
        )
    else:
        changed = kb.block_task(
            board, child, kind="needs_input", reason="RETURN: changed parent", **fence,
        )
    # The finalizer must refuse stale parent input before any terminal mutation.
    assert changed is False
    task = kb.get_task(board, child)
    assert task.status == "running" and task.current_run_id == run_id
    assert task.claim_lock is not None and task.worker_pid == os.getpid()
    assert board.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] == "running"
    assert kb.get_task(board, downstream).status == "todo"
    assert not board.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind IN ('completed','blocked')",
        (child,),
    ).fetchone()


@pytest.mark.parametrize("terminal", ["complete", "block"])
def test_matching_parent_handoff_allows_exact_child_finalization(board, terminal):
    parent, child, downstream, run_id, identity, witness = _launched_child(board)
    assert parent == witness[0]["task_id"]
    fence = dict(expected_run_id=run_id, expected_worker_pid=os.getpid(),
                 expected_board_identity=identity, expected_parent_handoffs=witness)
    if terminal == "complete":
        changed = kb.complete_task(board, child, summary="verified child output",
                                   fire_lifecycle_hook=False, **fence)
    else:
        changed = kb.block_task(board, child, kind="needs_input",
                                reason="RETURN: verified parent", **fence)
    assert changed is True
    assert kb.get_task(board, child).status == ("done" if terminal == "complete" else "blocked")
    assert kb.get_task(board, downstream).status == ("ready" if terminal == "complete" else "todo")


def test_empty_parent_witness_requires_no_direct_parent(board):
    child = kb.create_task(board, title="root child", assignee="maker")
    generation = board.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (child,)).fetchone()[0]
    identity = board_identity(board)
    launched = kbd.dispatch_once(
        board, spawn_fn=lambda task, workspace, board=None: os.getpid(),
        max_spawn=1, max_in_progress=1, max_in_progress_per_profile=1,
        board="default", task_id=child, lane="ready",
        expected_task_event_id=generation, expected_board_identity=identity,
        expected_assignee="maker",
    )
    assert launched.selection_rejected is None
    run_id = int(launched.spawned_run_ids[child])
    fence = dict(expected_run_id=run_id, expected_worker_pid=os.getpid(),
                 expected_board_identity=identity, expected_parent_handoffs=[])
    with kbc.connect_closing(board="default") as other:
        parent = kb.create_task(other, title="new parent", assignee="maker")
        assert kb.complete_task(other, parent, summary="parent", fire_lifecycle_hook=False)
        other.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (parent, child))
        other.commit()
    assert kb.complete_task(board, child, summary="verified child output",
                            fire_lifecycle_hook=False, **fence) is False
    assert kb.get_task(board, child).status == "running"
    board.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (parent, child))
    board.commit()
    assert kb.complete_task(board, child, summary="verified child output",
                            fire_lifecycle_hook=False, **fence) is True
