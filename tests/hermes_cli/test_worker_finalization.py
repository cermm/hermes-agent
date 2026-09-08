"""A verified host cannot finalize a superseded or independently stopped worker."""
import os

import pytest


@pytest.fixture
def native(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "default" / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db()
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="inert controlled worker", assignee="fixture")
        claimed = kb.claim_task(conn, task, claimer="fixture")
        conn.execute("UPDATE task_runs SET worker_pid=? WHERE id=?", (os.getpid(), claimed.current_run_id))
        conn.commit()
        yield kb, conn, task, claimed.current_run_id


def test_wrong_process_or_run_cannot_finalize(native):
    kb, conn, task, run = native
    assert not kb.complete_task(conn, task, expected_run_id=run, expected_worker_pid=os.getpid()+1, fire_lifecycle_hook=False)
    assert not kb.block_task(conn, task, expected_run_id=run+1, expected_worker_pid=os.getpid())
    assert kb.get_task(conn, task).status == "running"


def test_operator_block_is_not_overwritten_by_late_worker_result(native):
    kb, conn, task, run = native
    assert kb.block_task(conn, task, expected_run_id=run, reason="operator stopped")
    assert not kb.complete_task(conn, task, expected_run_id=run, expected_worker_pid=os.getpid(), fire_lifecycle_hook=False)
    assert kb.get_task(conn, task).status == "blocked"


def test_exact_running_worker_completes_once(native):
    kb, conn, task, run = native
    assert kb.complete_task(conn, task, expected_run_id=run, expected_worker_pid=os.getpid(), fire_lifecycle_hook=False)
    assert not kb.complete_task(conn, task, expected_run_id=run, expected_worker_pid=os.getpid(), fire_lifecycle_hook=False)
    assert kb.get_task(conn, task).status == "done"


def test_exact_running_worker_can_record_controlled_failure(native):
    kb, conn, task, run = native
    assert kb.block_task(conn, task, expected_run_id=run, expected_worker_pid=os.getpid(), reason="inert controlled failure")
    assert not kb.block_task(conn, task, expected_run_id=run, expected_worker_pid=os.getpid(), reason="late overwrite")


@pytest.mark.parametrize('operation', ['complete', 'block'])
def test_replaced_board_with_same_task_run_and_pid_cannot_be_finalized(native, tmp_path, operation):
    import sqlite3
    from hermes_cli.kanban_db_identity import board_identity
    kb, conn, task, run = native
    expected = board_identity(conn)
    with sqlite3.connect(tmp_path / 'restored.sqlite') as restored:
        restored.row_factory = sqlite3.Row
        conn.backup(restored)
        assert restored.execute('SELECT worker_pid FROM task_runs WHERE id=?', (run,)).fetchone()[0] == os.getpid()
        arguments = dict(expected_run_id=run, expected_worker_pid=os.getpid(), expected_board_identity=expected)
        if operation == 'complete':
            changed = kb.complete_task(restored, task, fire_lifecycle_hook=False, **arguments)
        else:
            changed = kb.block_task(restored, task, **arguments)
        assert not changed
        assert kb.get_task(restored, task).status == 'running'


def test_completion_rechecks_board_identity_inside_write_transaction(native, monkeypatch):
    from contextlib import contextmanager
    from hermes_cli.kanban_db_identity import board_identity
    kb, conn, task, run = native
    expected = board_identity(conn)
    original = kb.write_txn
    @contextmanager
    def changed_lineage(connection, **kwargs):
        with original(connection, **kwargs):
            connection.execute("UPDATE task_events SET payload='{}' WHERE id=(SELECT MIN(id) FROM task_events)")
            yield
    monkeypatch.setattr(kb, 'write_txn', changed_lineage)
    assert not kb.complete_task(conn, task, expected_run_id=run, expected_worker_pid=os.getpid(),
                                expected_board_identity=expected, fire_lifecycle_hook=False)
    assert kb.get_task(conn, task).status == 'running'
