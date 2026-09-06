"""Compare-and-save ownership for goal evaluations that wait on external work."""

from contextlib import nullcontext


class GoalEvaluationSuperseded(Exception):
    """A later user mutation or cancellation owns the durable goal now."""


class GoalEvaluationOwnership:
    def __init__(self, db, key, state, *, is_current=None, write_lock=None):
        self.db = db
        self.key = key
        self.is_current = is_current or (lambda: True)
        self.write_lock = write_lock
        self.expected = db.get_meta(key) if db is not None else None
        if (not self.expected or not self.is_current()
                or type(state).from_json(self.expected).to_json() != state.to_json()):
            raise GoalEvaluationSuperseded()

    def save(self, state):
        # Never hold the cancellation lock across the judge, a shell, or an
        # approval wait. Only the atomic durable write and cancellation compete.
        with self.write_lock if self.write_lock is not None else nullcontext():
            if not self.is_current():
                raise GoalEvaluationSuperseded()
            value = state.to_json()
            if not self.db.compare_and_set_meta(self.key, self.expected, value):
                raise GoalEvaluationSuperseded()
            self.expected = value
