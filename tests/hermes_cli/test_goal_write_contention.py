"""A foreign SQLite writer cannot pin goal cancellation behind a long retry."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from hermes_cli import goals
from tests.gateway.test_goal_lifecycle_ownership import hermes_home


def test_busy_goal_save_releases_cancellation_lock(hermes_home, monkeypatch):
    manager = goals.GoalManager('busy-goal')
    manager.set('preserve cancellation responsiveness')
    db = goals._get_session_db()
    before = goals.load_goal(manager.session_id)
    database_path = db._conn.execute('PRAGMA database_list').fetchone()[2]
    entered, cancelled = threading.Event(), threading.Event()
    control_lock = threading.RLock()
    compare_and_set = db.compare_and_set_meta

    def observed_write(*args, **kwargs):
        entered.set()
        return compare_and_set(*args, **kwargs)

    def cancel():
        with control_lock:
            cancelled.set()

    monkeypatch.setattr(db, 'compare_and_set_meta', observed_write)
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **kw: ('done', 'done', False, None, False))
    blocker = sqlite3.connect(database_path, timeout=0.1)
    blocker.execute('BEGIN IMMEDIATE')
    with ThreadPoolExecutor(max_workers=2) as workers:
        write = workers.submit(manager.evaluate_after_turn, 'finished',
                               is_current=lambda: not cancelled.is_set(), write_lock=control_lock)
        try:
            assert entered.wait(3)
            cancellation = workers.submit(cancel)
            # The existing long metadata retry budget would hold this lock for
            # 20 seconds while the foreign writer remains live.
            cancellation.result(timeout=3)
            with pytest.raises(sqlite3.OperationalError, match='locked|busy'):
                write.result(timeout=3)
            assert goals.load_goal(manager.session_id) == before
        finally:
            blocker.rollback()
            blocker.close()
