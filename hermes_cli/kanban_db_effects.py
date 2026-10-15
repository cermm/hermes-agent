"""Dispatch custody in canonical run metadata; no separate journal or scheduler."""
from __future__ import annotations

import json


def set_dispatch_phase(conn, task_id, run_id, claim_lock, phase, *, prior=None):
    """Write exact-owner custody before callback entry or after observation."""
    from . import kanban_db as kb

    if phase not in {"not_entered", "entering", "unknown", "settled"}:
        raise ValueError("invalid dispatch phase")
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT t.status,t.current_run_id,t.claim_lock,r.status AS run_status,"
            "r.claim_lock AS run_claim_lock,r.metadata FROM tasks t "
            "JOIN task_runs r ON r.id=t.current_run_id AND r.task_id=t.id "
            "WHERE t.id=? AND r.id=?", (task_id, run_id),
        ).fetchone()
        if (row is None or row["status"] != "running" or row["run_status"] != "running"
                or row["claim_lock"] != claim_lock or row["run_claim_lock"] != claim_lock):
            return False
        try:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        except (TypeError, ValueError):
            return False
        if not isinstance(metadata, dict) or metadata.get("dispatch_effect_phase") != prior:
            return False
        metadata["dispatch_effect_phase"] = phase
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=? AND status='running'",
                     (json.dumps(metadata, allow_nan=False), run_id))
    return True


def mark_dispatch_unknown(conn, task_id, run_id, claim_lock, failure_type, *, prior):
    """Preserve the entered effect even if its owner changed before readback."""
    from . import kanban_db as kb

    updated = set_dispatch_phase(conn, task_id, run_id, claim_lock, "unknown", prior=prior)
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "dispatch_effect_unknown",
                         {"failure_type": failure_type, "claim_lock": claim_lock}, run_id=run_id)
    return updated


def pending_dispatch_effect(conn, task_id, run_id):
    """Refuse recovery of a witnessed run until authoritative retirement.

    Legacy claims without a custody declaration retain their recovery behavior.
    An exact-run unknown event still holds when its metadata was lost or damaged.
    A changed run is never permission to reclaim the replacement.
    """
    row = conn.execute(
        "SELECT t.status,t.current_run_id,t.claim_lock,t.claim_expires,t.worker_pid,"
        "r.status AS run_status,r.claim_lock AS run_claim_lock,"
        "r.claim_expires AS run_expires,r.worker_pid AS run_pid,"
        "r.ended_at,r.metadata FROM tasks t "
        "LEFT JOIN task_runs r ON r.id=t.current_run_id AND r.task_id=t.id WHERE t.id=?",
        (task_id,),
    ).fetchone()
    if row is None or row["current_run_id"] != run_id:
        return True
    unknown = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND run_id IS ? "
        "AND kind='dispatch_effect_unknown' LIMIT 1", (task_id, run_id),
    ).fetchone()
    if unknown:
        return True
    try:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    except (TypeError, ValueError):
        return True
    if not isinstance(metadata, dict):
        return True
    if "dispatch_effect_phase" not in metadata:
        return False
    if metadata["dispatch_effect_phase"] != "not_entered":
        return True
    return (row["status"] != "running" or row["run_status"] != "running"
            or not row["claim_lock"] or row["claim_lock"] != row["run_claim_lock"]
            or row["claim_expires"] is None or row["claim_expires"] != row["run_expires"]
            or row["worker_pid"] is not None or row["run_pid"] is not None
            or row["ended_at"] is not None)
