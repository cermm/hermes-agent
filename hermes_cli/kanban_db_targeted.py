"""Final admission for an already-claimed exact target."""
from __future__ import annotations

import time

from hermes_cli.kanban_db_identity import board_identity, database_file_path


class TargetAdmissionHeld(RuntimeError):
    """A definite pre-effect refusal; no spawn callback was entered."""


def require_live_claim(conn, claimed):
    """The caller holds a write transaction; a refusal precedes callback entry."""
    row = conn.execute(
        "SELECT status, current_run_id, claim_lock, claim_expires FROM tasks WHERE id = ?",
        (claimed.id,),
    ).fetchone()
    if row is None or row["status"] != "running" or row["current_run_id"] != claimed.current_run_id or not row["claim_lock"] or row["claim_lock"] != claimed.claim_lock:
        raise TargetAdmissionHeld("target_claim_changed")
    if row["claim_expires"] is None or row["claim_expires"] <= int(time.time()):
        raise TargetAdmissionHeld("target_claim_expired")


def spawn_targeted(conn, claimed, snapshot, workspace, board, lane, expected_identity, spawn_fn, branch_name=None, *, max_spawn=None, max_in_progress=None, per_profile_cap=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as dispatch

    # Other Kanban writers cannot alter admission between these checks and
    # entering the process boundary. Filesystem replacement/restore outside
    # the cooperating board lifecycle remains a separately documented limit.
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (claimed.id,)).fetchone()
        ignored = {"last_heartbeat_at", "claim_expires", "_claim_event_id"}
        if row is None or row["status"] != "running" or row["current_run_id"] != claimed.current_run_id or row["claim_lock"] != claimed.claim_lock or row["assignee"] != claimed.assignee or any(row[key] != value for key, value in snapshot.items() if key not in ignored):
            raise TargetAdmissionHeld("target_changed_after_claim")
        if database_file_path(conn) != kb.kanban_db_path(board=board).resolve():
            raise TargetAdmissionHeld("board_connection_changed_after_claim")
        if board_identity(conn) != expected_identity:
            raise TargetAdmissionHeld("board_identity_changed_after_claim")
        if not kb._parents_satisfied(conn, claimed.id):
            raise TargetAdmissionHeld("dependency_changed_after_claim")
        if lane == "review" and not dispatch.review_dispatch_enabled():
            raise TargetAdmissionHeld("review_disabled_after_claim")
        exists = dispatch._profile_exists_fn()
        if exists is None or not exists(claimed.assignee):
            raise TargetAdmissionHeld("profile_unavailable_after_claim")
        running = dispatch.count_running_tasks(conn)
        if max_spawn is not None and running > max_spawn:
            raise TargetAdmissionHeld("capacity_changed_after_claim")
        if max_in_progress is not None and running + dispatch.count_running_tasks_other_boards(board) > max_in_progress:
            raise TargetAdmissionHeld("host_capacity_changed_after_claim")
        if per_profile_cap is not None:
            profile_running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running' AND assignee = ?", (claimed.assignee,)).fetchone()[0]
            if profile_running > per_profile_cap:
                raise TargetAdmissionHeld("profile_capacity_changed_after_claim")
        if dispatch._memory_pressure_level() == "critical":
            raise TargetAdmissionHeld("memory_pressure_after_claim")
        reason = dispatch.check_respawn_guard(conn, claimed.id, lane=lane)
        if reason:
            raise TargetAdmissionHeld(reason)
        # A new comment or task mutation also invalidates the claimed packet.
        extra = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? "
            "AND kind != 'tip_scratch_workspace' LIMIT 1",
            (claimed.id, snapshot["_claim_event_id"]),
        ).fetchone()
        if extra:
            raise TargetAdmissionHeld("target_event_after_claim")
        require_live_claim(conn, claimed)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (workspace, claimed.id))
        if branch_name is not None:
            conn.execute("UPDATE tasks SET branch_name = ? WHERE id = ?", (branch_name, claimed.id))
        claimed.workspace_path = workspace
        if branch_name is not None:
            claimed.branch_name = branch_name
        return dispatch._call_spawn_fn(spawn_fn, claimed, workspace, board)
