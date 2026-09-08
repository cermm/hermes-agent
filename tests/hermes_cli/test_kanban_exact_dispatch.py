"""Exact dispatch selection keeps competitors and stale generations inert."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_parser import build_parser
from hermes_cli.kanban_db_identity import board_identity


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda name: True)
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def generation(conn, task_id):
    return conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_cli_exact_selection_spawns_only_requested_generation(board, monkeypatch, capsys, lane):
    competitor = kb.create_task(board, title="higher priority", assignee="qa", priority=100)
    target = kb.create_task(board, title="requested", assignee="qa")
    if lane == "review":
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status = ? WHERE id = ?", (lane, target))
            kb._append_event(board, target, "review_requested", {})
    expected = generation(board, target)
    identity = board_identity(board)
    spawned = []
    monkeypatch.setattr(dispatch, "_default_spawn", lambda task, workspace, board=None: spawned.append(task.id))
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    show_args = parser.parse_args(["kanban", "--board", "default", "show", target, "--json"])
    assert cli.kanban_command(show_args) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["board"] == "default"
    assert observed["board_identity"] == identity
    assert observed["task_generation"] == expected
    assert max(event["id"] for event in observed["events"]) == expected
    args = parser.parse_args(["kanban", "--board", "default", "dispatch", "--max", "1",
                              "--task-id", target, "--lane", lane,
                              "--expected-task-event-id", str(expected), "--expected-board-identity", identity,
                              "--expected-assignee", "qa", "--json"])
    assert cli.kanban_command(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert spawned == [target]
    assert receipt["board"] == "default"
    assert receipt["board_identity"] == identity
    assert receipt["effect_status"] == "spawned"
    assert receipt["dry_run"] is False
    assert receipt["would_spawn"] == []
    assert receipt["no_spawn_reason"] is None
    assert receipt["selection"] == {"task_id": target, "lane": lane,
        "expected_task_event_id": expected, "expected_board_identity": identity, "expected_assignee": "qa"}
    assert receipt["spawned"][0]["task_id"] == target
    assert receipt["spawned"][0]["run_id"] == kb.get_task(board, target).current_run_id
    assert kb.get_task(board, competitor).status == "ready"
    # Replaying the old selection never reclaims running work or dispatches a competitor.
    replay = dispatch.dispatch_once(board, task_id=target, lane=lane,
                                    expected_task_event_id=expected, expected_board_identity=identity, expected_assignee="qa", spawn_fn=lambda *a: pytest.fail("stale spawn"))
    assert replay.selection_rejected == "stale_target_selection"
    assert spawned == [target]
    assert kb.get_task(board, competitor).current_run_id is None


@pytest.mark.parametrize("gate", ["stale", "claim_race", "dependency", "blocked", "review_disabled", "capacity", "unassigned", "respawn"])
def test_targeted_dispatch_preserves_gates_without_fallback(board, monkeypatch, gate):
    competitor = kb.create_task(board, title="competitor", assignee="qa", priority=100)
    target = kb.create_task(board, title="target", assignee=None if gate == "unassigned" else "qa")
    expected = generation(board, target)
    identity = board_identity(board)
    lane = "ready"
    kwargs = {}
    if gate == "stale":
        kb.add_comment(board, target, "operator", "new steering invalidates generation")
    elif gate == "claim_race":
        original = kb.claim_task
        def racing_claim(conn, task_id, **kw):
            kb.add_comment(conn, task_id, "operator", "changes between selection and claim")
            return original(conn, task_id, **kw)
        monkeypatch.setattr(kb, "claim_task", racing_claim)
    elif gate == "dependency":
        parent = kb.create_task(board, title="incomplete parent")
        kb.link_tasks(board, parent, target)
        # A competing writer incorrectly forces ready. The atomic claim must recheck parents.
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (target,))
        expected = generation(board, target)
    elif gate == "blocked":
        kb.block_task(board, target, reason="human approval required")
    elif gate == "review_disabled":
        lane = "review"
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (target,))
        monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: False)
    elif gate == "capacity":
        kwargs["max_spawn"] = 0
    elif gate == "respawn":
        monkeypatch.setattr(dispatch, "check_respawn_guard", lambda *a, **kw: "approval hold")
    result = dispatch.dispatch_once(
        board, task_id=target, lane=lane, expected_task_event_id=expected,
        expected_board_identity=identity, expected_assignee="qa",
        spawn_fn=lambda *a: pytest.fail("a gate released worker execution"),
        default_assignee="qa", **kwargs,
    )
    assert result.spawned == []
    assert kb.get_task(board, target).current_run_id is None
    assert kb.get_task(board, competitor).status == "ready"
    assert kb.get_task(board, competitor).current_run_id is None


@pytest.mark.parametrize("change", ["block", "comment", "assignee", "dependency", "review_disabled", "capacity"])
def test_target_mutation_after_claim_cannot_cross_spawn_boundary(board, monkeypatch, change):
    competitor = kb.create_task(board, title="unrelated", assignee="qa", priority=100)
    parent = kb.create_task(board, title="unfinished")
    target = kb.create_task(board, title="target", assignee="qa")
    lane = "review" if change == "review_disabled" else "ready"
    if lane == "review":
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (target,))
    expected = generation(board, target)
    identity = board_identity(board)
    original = dispatch._kbw.resolve_workspace

    def resolving(task, **kwargs):
        workspace = original(task, **kwargs)
        if change == "block":
            kb.block_task(board, target, kind="needs_input", reason="human approval")
        elif change == "comment":
            kb.add_comment(board, target, "operator", "new candidate instructions")
        elif change == "assignee":
            with kb.write_txn(board):
                board.execute("UPDATE tasks SET assignee = 'other' WHERE id = ?", (target,))
        elif change == "dependency":
            kb.link_tasks(board, parent, target)
        elif change == "review_disabled":
            monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: False)
        elif change == "capacity":
            kb.claim_task(board, competitor)
        return workspace

    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", resolving)
    result = dispatch.dispatch_once(board, task_id=target, lane=lane,
        expected_task_event_id=expected, expected_board_identity=identity,
        expected_assignee="qa", max_spawn=1, spawn_fn=lambda *a: pytest.fail("stale admission spawned"))
    assert result.spawned == []
    assert result.effect_unknown is None
    assert result.no_spawn_reason
    if change != "capacity":
        assert kb.get_task(board, competitor).status == "ready"
        assert kb.get_task(board, competitor).current_run_id is None


@pytest.mark.parametrize("failure", [TypeError, ValueError, RuntimeError])
def test_spawn_callback_exception_is_once_and_requires_reconciliation(board, failure):
    target = kb.create_task(board, title="target", assignee="qa")
    calls = []
    def started_then_failed(task, workspace, board=None):
        calls.append(task.id)
        raise failure("may have started a process")
    result = dispatch.dispatch_once(board, task_id=target, lane="ready",
        expected_task_event_id=generation(board, target), expected_board_identity=board_identity(board),
        expected_assignee="qa", spawn_fn=started_then_failed)
    assert calls == [target]
    assert result.effect_unknown == failure.__name__
    assert kb.get_task(board, target).status == "blocked"
    retry = dispatch.dispatch_once(board, spawn_fn=lambda *a: pytest.fail("ambiguous run retried"),
                                   reconcile_orphans=False)
    assert retry.spawned == []


def test_same_task_revision_in_replaced_board_file_is_rejected(board, tmp_path):
    import os
    import sqlite3
    from hermes_cli.kanban_db_identity import database_file_path
    target = kb.create_task(board, title="target", assignee="qa")
    expected = generation(board, target)
    identity = board_identity(board)
    path = database_file_path(board)
    replacement = tmp_path / "replacement.db"
    with sqlite3.connect(replacement) as copied:
        board.backup(copied)
    board.close()
    os.replace(replacement, path)
    with kbc.connect_closing() as replaced:
        assert generation(replaced, target) == expected
        assert board_identity(replaced) != identity
        result = dispatch.dispatch_once(replaced, task_id=target, lane="ready",
            expected_task_event_id=expected, expected_board_identity=identity,
            expected_assignee="qa", spawn_fn=lambda *a: pytest.fail("replaced board spawned"))
        assert result.selection_rejected == "stale_target_selection"
        assert kb.get_task(replaced, target).status == "ready"


def test_targeted_api_rejects_connection_for_a_different_worker_board(board):
    target = kb.create_task(board, title="target", assignee="qa")
    result = dispatch.dispatch_once(board, board="other", task_id=target, lane="ready",
        expected_task_event_id=generation(board, target), expected_board_identity=board_identity(board),
        expected_assignee="qa", spawn_fn=lambda *a: pytest.fail("wrong worker board admitted"))
    assert result.selection_rejected == "board_connection_mismatch"
    assert kb.get_task(board, target).status == "ready"


@pytest.mark.parametrize("stage", ["spawn", "pid", "hook"])
def test_board_wake_unknown_effect_never_requeues_or_continues_tick(board, monkeypatch, stage):
    target = kb.create_task(board, title="first", assignee="qa", priority=100)
    competitor = kb.create_task(board, title="second", assignee="qa")
    calls = []
    def fail(*args, **kwargs):
        raise RuntimeError("after process admission")
    def started(task, workspace, board=None):
        calls.append(task.id)
        if stage == "spawn":
            fail()
        return 999999
    if stage == "pid":
        monkeypatch.setattr(dispatch, "_set_worker_pid", fail)
    elif stage == "hook":
        monkeypatch.setattr(kb, "_fire_worker_spawned_hook", fail)
    result = dispatch.dispatch_once(board, spawn_fn=started, max_spawn=2, reconcile_orphans=False)
    assert result.effect_unknown == "RuntimeError"
    assert calls == [target]
    assert kb.get_task(board, target).status == "blocked"
    assert kb.get_task(board, target).block_kind == "needs_input"
    assert kb.get_task(board, competitor).status == "ready"
    # Remove unrelated ready work from the fixture; the ambiguous target alone
    # must remain parked through the real legacy reclaim/promote pass.
    kb.block_task(board, competitor, kind="needs_input", reason="fixture hold")
    again = dispatch.dispatch_once(board, spawn_fn=lambda *a: pytest.fail("unknown effect retried"))
    assert again.spawned == []
    assert kb.get_task(board, target).status == "blocked"


@pytest.mark.parametrize("field", ["workspace_path", "branch_name"])
def test_exact_admission_rejects_workspace_steering_during_resolution(board, monkeypatch, tmp_path, field):
    target = kb.create_task(board, title="target", assignee="qa")
    expected = generation(board, target)
    identity = board_identity(board)
    original = dispatch._kbw.resolve_workspace

    def changed_workspace(task, **kwargs):
        workspace = original(task, **kwargs)
        setter = dispatch._kbw.set_workspace_path if field == "workspace_path" else dispatch._kbw.set_branch_name
        setter(board, target, str(tmp_path / "operator-selected"))
        return workspace

    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", changed_workspace)
    calls = []
    result = dispatch.dispatch_once(board, task_id=target, lane="ready",
        expected_task_event_id=expected, expected_board_identity=identity,
        expected_assignee="qa", spawn_fn=lambda *args: calls.append(target))
    assert calls == []
    assert result.no_spawn_reason == "target_changed_after_claim"


@pytest.mark.parametrize("targeted", [True, False])
def test_real_default_spawn_receives_resolved_branch(board, monkeypatch, tmp_path, targeted):
    from types import SimpleNamespace
    profile = tmp_path / "hermes" / "profiles" / "qa"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("{}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = kb.create_task(board, title="worktree target", assignee="qa", workspace_kind="worktree")
    expected = generation(board, target)
    identity = board_identity(board)
    monkeypatch.setattr(dispatch._kbw, "_resolve_worktree_workspace",
                        lambda *args, **kwargs: (workspace, "wt/resolved-branch"))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(dispatch, "_resolve_hermes_argv", lambda: ["inert-hermes"])
    calls = []
    def inert_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=424242)
    monkeypatch.setattr(dispatch.subprocess, "Popen", inert_popen)
    selection = dict(task_id=target, lane="ready", expected_task_event_id=expected,
                     expected_board_identity=identity, expected_assignee="qa") if targeted else {}
    result = dispatch.dispatch_once(board, **selection)
    assert result.effect_unknown is None
    assert len(calls) == 1
    env = calls[0][1]["env"]
    assert env.get("HERMES_KANBAN_BRANCH") == "wt/resolved-branch"
    assert env["HERMES_KANBAN_DB"] == str(kb.kanban_db_path())
    assert env["HERMES_KANBAN_RUN_ID"] == str(result.spawned_run_ids[target])
    assert env["HERMES_PROFILE"] == "qa"
    assert env["HERMES_HOME"] == str(profile)
    assert kb.get_task(board, target).workspace_path == str(workspace)


@pytest.mark.parametrize("expiry", [1, None])
@pytest.mark.parametrize("targeted", [True, False])
def test_target_with_expired_or_missing_lease_never_spawns(board, monkeypatch, expiry, targeted):
    target = kb.create_task(board, title="slow preparation", assignee="qa")
    expected = generation(board, target)
    identity = board_identity(board)
    original = dispatch._kbw.resolve_workspace

    def delayed_resolution(task, **kwargs):
        workspace = original(task, **kwargs)
        # Represent a preparation that outlived its lease, without real sleeps.
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (expiry, target))
        return workspace

    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", delayed_resolution)
    calls = []
    selection = dict(task_id=target, lane="ready", expected_task_event_id=expected,
                     expected_board_identity=identity, expected_assignee="qa") if targeted else {}
    result = dispatch.dispatch_once(board, **selection, spawn_fn=lambda *args: calls.append(target))
    assert calls == []
    assert result.no_spawn_reason == "target_claim_expired"
    assert result.effect_unknown is None
    assert kb.get_task(board, target).status == "blocked"
    retry = dispatch.dispatch_once(board, spawn_fn=lambda *a: pytest.fail("expired target retried"),
                                   reconcile_orphans=False)
    assert retry.spawned == []


@pytest.mark.parametrize("targeted", [True, False])
@pytest.mark.parametrize("stage", ["callback", "hook"])
def test_post_effect_admission_exception_is_never_a_no_spawn_receipt(board, monkeypatch, targeted, stage):
    from hermes_cli.kanban_db_targeted import TargetAdmissionHeld
    target = kb.create_task(board, title="first", assignee="qa", priority=100)
    competitor = kb.create_task(board, title="other", assignee="qa")
    selection = dict(task_id=target, lane="ready", expected_task_event_id=generation(board, target),
                     expected_board_identity=board_identity(board), expected_assignee="qa") if targeted else {}
    calls = []
    def spawned(task, workspace, board=None):
        calls.append(task.id)
        if stage == "callback":
            raise TargetAdmissionHeld("admission failure raised after callback side effect")
        return 424242
    if stage == "hook":
        def hook(*args, **kwargs):
            raise TargetAdmissionHeld("hook failed after process creation")
        monkeypatch.setattr(kb, "_fire_worker_spawned_hook", hook)
    result = dispatch.dispatch_once(board, **selection, spawn_fn=spawned, max_spawn=1)
    assert calls == [target]
    assert result.effect_unknown
    assert not result.no_spawn_reason
    assert kb.get_task(board, competitor).status == "ready"


def test_legacy_lost_claim_cannot_overwrite_successor_workspace(board, monkeypatch, tmp_path):
    target = kb.create_task(board, title="worktree", assignee="qa", workspace_kind="worktree")
    original_workspace = tmp_path / "original"
    successor_workspace = tmp_path / "successor"
    original_workspace.mkdir()
    successor_workspace.mkdir()
    successor = []

    def superseded(task, **kwargs):
        assert kb.block_task(board, target, reason="operator replans work")
        assert kb.unblock_task(board, target)
        successor.append(kb.claim_task(board, target))
        dispatch._kbw.set_workspace_path(board, target, successor_workspace)
        dispatch._kbw.set_branch_name(board, target, "wt/successor")
        return original_workspace, "wt/original"

    monkeypatch.setattr(dispatch._kbw, "_resolve_worktree_workspace", superseded)
    result = dispatch.dispatch_once(board, spawn_fn=lambda *a: pytest.fail("lost claim spawned"))
    current = kb.get_task(board, target)
    assert current.current_run_id == successor[0].current_run_id
    assert current.status == "running"
    assert current.workspace_path == str(successor_workspace)
    assert current.branch_name == "wt/successor"
    assert result.no_spawn_reason == "target_claim_changed"

@pytest.mark.parametrize("targeted", [False, True])
def test_cli_dry_run_reports_candidates_without_spawn_effect(board, monkeypatch, capsys, targeted):
    target = kb.create_task(board, title="eligible preview", assignee="qa")
    expected = generation(board, target)
    identity = board_identity(board)
    before = list(board.iterdump())
    monkeypatch.setattr(dispatch, "_default_spawn", lambda *a, **kw: pytest.fail("dry run entered spawn"))
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    command = ["kanban", "--board", "default", "dispatch", "--max", "1", "--dry-run", "--json"]
    if targeted:
        command += ["--task-id", target, "--lane", "ready",
                    "--expected-task-event-id", str(expected),
                    "--expected-board-identity", identity, "--expected-assignee", "qa"]
    assert cli.kanban_command(parser.parse_args(command)) == 0
    receipt = json.loads(capsys.readouterr().out)
    # A preview of an eligible task must not masquerade as an actual worker receipt.
    assert receipt["effect_status"] == "not_spawned"
    assert receipt["dry_run"] is True
    assert receipt["effect_unknown"] is None
    assert receipt["no_spawn_reason"] == "dry_run"
    assert receipt["spawned"] == []
    assert receipt["would_spawn"] == [{"task_id": target, "assignee": "qa", "workspace": "", "run_id": None}]
    assert kb.get_task(board, target).status == "ready"
    assert kb.get_task(board, target).current_run_id is None
    assert list(board.iterdump()) == before
