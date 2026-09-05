"""Read-only review probe: a real turn clears its interrupt in finalization."""
import threading
from unittest.mock import patch

import pytest
from agent.interrupt_control import InterruptControlMixin
from tools.delegate_tool import _run_single_child


class Parent:
    _current_task_id = None
    _delegate_depth = 0

    def _touch_activity(self, _desc):
        pass


class FinalizingChild(InterruptControlMixin):
    tool_progress_callback = None
    _delegate_saved_tool_names = []
    _credential_pool = None
    _subagent_id = None
    _delegate_depth = 1
    _parent_subagent_id = None
    _delegate_output_schema = {'type': 'object', 'required': ['city'], 'properties': {'city': {'type': 'string'}}}
    _delegate_escalate_on_validation_failure = True
    _fallback_chain = ['repair-one', 'repair-two']
    model = 'initial'
    session_prompt_tokens = session_completion_tokens = session_reasoning_tokens = 0
    session_estimated_cost_usd = 0.0
    quiet_mode = True

    def __init__(self, blocked_turn):
        self.blocked_turn = blocked_turn
        self.calls = []
        self.finishing = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self._interrupt_requested = False
        self._hard_interrupt_requested = threading.Event()
        self._execution_thread_id = None
        self._active_children = []
        self._active_children_lock = threading.Lock()

    def get_activity_summary(self):
        return {'api_call_count': len(self.calls), 'max_iterations': 5, 'current_tool': None}

    def _try_activate_fallback(self):
        self.model = 'repair'
        return True

    def run_conversation(self, user_message, **kwargs):
        self.calls.append(user_message)
        # The terminal result is formed before clear_interrupt() in finalize_turn.
        result = {'final_response': 'invalid', 'completed': True, 'api_calls': 1, 'messages': []}
        if len(self.calls) == self.blocked_turn:
            self.finishing.set()
            assert self.release.wait(30)
        self.clear_interrupt()
        return result

    def close(self):
        self.closed.set()


@pytest.mark.parametrize('blocked_turn', [1, 2])
def test_timed_out_delegation_does_not_start_another_schema_turn(blocked_turn):
    child = FinalizingChild(blocked_turn)
    done = threading.Event()
    outcomes = []

    def parent_run():
        try:
            outcomes.append(_run_single_child(0, 'return city', child, Parent()))
        finally:
            done.set()

    with patch('tools.delegate_tool._get_child_timeout', return_value=2):
        parent_thread = threading.Thread(target=parent_run, daemon=True)
        parent_thread.start()
        try:
            assert child.finishing.wait(5)
            assert done.wait(15)
            assert outcomes[0]['status'] == 'timeout'
            assert len(child.calls) == blocked_turn
            assert child._hard_interrupt_requested.is_set()
        finally:
            child.release.set()
            parent_thread.join(10)
        assert child.closed.wait(5)
    assert len(child.calls) == blocked_turn, 'abandoned worker began a fresh repair after the parent returned timeout'
