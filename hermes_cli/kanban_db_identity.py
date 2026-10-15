"""Read-only identity witness for the connected board file.

This detects file replacement and changed first-event lineage. It deliberately
does not claim to distinguish an exact historical restore into the same inode;
a lifecycle-owned incarnation is required for that stronger guarantee.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3


def database_file_path(conn: sqlite3.Connection) -> Path:
    rows = conn.execute("PRAGMA database_list").fetchall()
    filename = next((row[2] for row in rows if row[1] == "main"), "")
    if not filename:
        raise ValueError("board_file_identity_unavailable")
    return Path(filename).resolve()


def board_identity(conn: sqlite3.Connection) -> str:
    path = database_file_path(conn)
    stat = path.stat()
    first = conn.execute(
        "SELECT id, task_id, kind, payload, created_at FROM task_events ORDER BY id LIMIT 1"
    ).fetchone()
    witness = {"schema": "kanban-board-file-identity-v1", "path": str(path),
               "device": stat.st_dev, "inode": stat.st_ino,
               "first_event": list(first) if first is not None else None}
    return hashlib.sha256(json.dumps(witness, sort_keys=True).encode()).hexdigest()


def target_snapshot_matches(conn, task_id, expected_event_id, expected_identity, expected_assignee):
    if expected_identity is not None and board_identity(conn) != expected_identity:
        return False
    row = conn.execute(
        "SELECT assignee, (SELECT MAX(id) FROM task_events WHERE task_id = tasks.id) AS generation "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return bool(row is not None and row["generation"] == expected_event_id
                and (expected_assignee is None or row["assignee"] == expected_assignee))


def task_intent_matches(conn, task_id, expected_task_intent):
    """Match launch-bound acceptance input inside the terminal write transaction."""
    if (not isinstance(expected_task_intent, dict)
            or set(expected_task_intent) != {"body", "assignee"}
            or not isinstance(expected_task_intent["body"], str)
            or not isinstance(expected_task_intent["assignee"], str)
            or not expected_task_intent["assignee"]):
        return False
    task = conn.execute("SELECT body,assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
    return bool(task is not None and (task["body"] or "") == expected_task_intent["body"]
                and task["assignee"] == expected_task_intent["assignee"])


def _worker_scope_matches(conn, task_id, expected_run_id, expected_worker_pid):
    if (type(expected_worker_pid) is not int or expected_worker_pid <= 0
            or expected_run_id is None):
        return False
    task = conn.execute("SELECT status,current_run_id,worker_pid FROM tasks WHERE id=?", (task_id,)).fetchone()
    if (task is None or task["status"] != "running" or task["current_run_id"] != int(expected_run_id)
            or task["worker_pid"] != expected_worker_pid):
        return False
    run = conn.execute("SELECT worker_pid,status FROM task_runs WHERE id=? AND task_id=?",
                       (int(expected_run_id), task_id)).fetchone()
    return bool(run is not None and run["worker_pid"] == expected_worker_pid and run["status"] == "running")


def terminal_scope_matches(conn, task_id, *, expected_run_id=None, expected_worker_pid=None,
                           expected_board_identity=None, expected_parent_handoffs=None,
                           expected_task_intent=None):
    """Check all launch witnesses on the terminal caller's write connection."""
    if expected_task_intent is not None and not task_intent_matches(conn, task_id, expected_task_intent):
        return False
    if expected_parent_handoffs is not None and not parent_handoffs_match(conn, task_id, expected_parent_handoffs):
        return False
    if expected_board_identity is not None and board_identity(conn) != expected_board_identity:
        return False
    if expected_worker_pid is not None and not _worker_scope_matches(conn, task_id, expected_run_id, expected_worker_pid):
        return False
    return True


def parent_handoffs_match(conn, child_task_id, expected_parent_handoffs):
    """Match the launch-time direct parent handoffs on this write connection.

    Call inside the child terminal write transaction. The producer's launch
    contract orders completed parent runs by started_at then id; use the same
    ordering, decoded metadata, and effective summary here.
    """
    if type(expected_parent_handoffs) is not list or len(expected_parent_handoffs) > 16:
        return False
    expected = {}
    for handoff in expected_parent_handoffs:
        if not isinstance(handoff, dict):
            return False
        parent_id = handoff.get("task_id")
        if not isinstance(parent_id, str) or not parent_id or parent_id in expected:
            return False
        if (handoff.get("status") != "done" or type(handoff.get("ended_at")) is not int
                or not isinstance(handoff.get("run_id"), str)
                or not handoff["run_id"].isdecimal()
                or not isinstance(handoff.get("summary"), str)
                or not isinstance(handoff.get("metadata"), dict)):
            return False
        expected[parent_id] = handoff
    direct_parent_ids = [row[0] for row in conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id",
        (child_task_id,),
    )]
    if direct_parent_ids != sorted(expected):
        return False
    for parent_id in direct_parent_ids:
        parent = conn.execute(
            "SELECT status,result FROM tasks WHERE id=?", (parent_id,),
        ).fetchone()
        if parent is None or parent[0] != "done":
            return False
        run = conn.execute(
            "SELECT id,status,ended_at,summary,metadata FROM task_runs "
            "WHERE task_id=? AND outcome='completed' "
            "ORDER BY started_at DESC,id DESC LIMIT 1", (parent_id,),
        ).fetchone()
        if run is None or run[1] != "done" or type(run[2]) is not int:
            return False
        handoff = expected[parent_id]
        if (str(run[0]) != handoff["run_id"] or run[2] != handoff["ended_at"]
                or (run[3] or parent[1] or "") != handoff["summary"]):
            return False
        try:
            metadata = json.loads(run[4]) if run[4] else {}
            canonical_actual = json.dumps(metadata, sort_keys=True, ensure_ascii=False, allow_nan=False)
            canonical_expected = json.dumps(handoff["metadata"], sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return False
        if not isinstance(metadata, dict) or canonical_actual != canonical_expected:
            return False
    return True
