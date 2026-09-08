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
