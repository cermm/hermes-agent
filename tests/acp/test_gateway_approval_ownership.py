from tools import approval_context, approval_gateway_wait, approval_prompt, approval_smart
"""Tests for GHSA-96vc-wcxf-jjff and GHSA-qg5c-hvr5-hjgr.

Two related ACP approval-flow issues:
- 96vc: ACP didn't set HERMES_EXEC_ASK, so `check_all_command_guards`
  took the non-interactive auto-approve path and never consulted the
  ACP-supplied callback.
- qg5c: `_approval_callback` was a module-global in terminal_tool;
  overlapping ACP sessions overwrote each other's callback slot.

Both fixed together by:
1. Setting HERMES_EXEC_ASK inside _run_agent (wraps the agent call).
2. Storing the callback in thread-local state so concurrent executor
   threads don't collide.
"""
import threading
import pytest

@pytest.fixture(autouse=True)
def _isolate_approval_state(monkeypatch):
    """Keep approval-isolation regressions independent of user config.

    The real developer profile may carry broad permanent/session allowlist
    entries. These tests assert routing behavior, so ambient approvals must not
    short-circuit the dangerous-command path before the callback/gateway owner
    logic under test runs.
    """
    from tools import approval
    permanent = set(approval._permanent_approved)
    session = {key: set(value) for key, value in approval._session_approved.items()}
    approval._permanent_approved.clear()
    approval._session_approved.clear()
    monkeypatch.setattr(approval_context, '_get_approval_mode', lambda: 'manual')
    try:
        yield
    finally:
        approval._permanent_approved.clear()
        approval._permanent_approved.update(permanent)
        approval._session_approved.clear()
        approval._session_approved.update(session)

class TestGatewayNotifierOwnership:

    def test_old_cleanup_preserves_replacement_notifier_and_queue(self):
        from tools import approval
        session_key = 'replacement-notifier-session'
        old_cb = lambda _data: None
        replacement_cb = lambda _data: None
        old_token = approval.register_gateway_notify(session_key, old_cb)
        replacement_token = approval.register_gateway_notify(session_key, replacement_cb)
        old_entry = approval_gateway_wait._ApprovalEntry({}, owner_token=old_token)
        replacement_entry = approval_gateway_wait._ApprovalEntry({}, owner_token=replacement_token)
        approval._gateway_queues[session_key] = [old_entry, replacement_entry]
        try:
            approval.unregister_gateway_notify(session_key, old_token)
            assert approval._gateway_notify_cbs[session_key] is replacement_cb
            assert approval._gateway_queues[session_key] == [replacement_entry]
            assert old_entry.event.is_set() is True
            assert replacement_entry.event.is_set() is False
        finally:
            approval.unregister_gateway_notify(session_key, replacement_token)

    def test_owner_change_between_enqueue_and_notify_drops_stale_prompt(self, monkeypatch):
        from tools import approval
        session_key = 'stale-notify-before-user-prompt-session'
        old_notified = []
        replacement_notified = []
        replacement = {}

        def old_cb(data):
            old_notified.append(data)

        def replacement_cb(data):
            replacement_notified.append(data)
        old_token = approval.register_gateway_notify(session_key, old_cb)
        original_fire_hook = approval_context._fire_approval_hook

        def _replace_owner_on_pre_hook(event, **kwargs):
            result = original_fire_hook(event, **kwargs)
            if event == 'pre_approval_request':
                approval.unregister_gateway_notify(session_key, old_token)
                replacement_token = approval.register_gateway_notify(session_key, replacement_cb)
                replacement_entry = approval_gateway_wait._ApprovalEntry({'command': 'replacement command', 'pattern_key': 'replacement'}, owner_token=replacement_token)
                approval._gateway_queues.setdefault(session_key, []).append(replacement_entry)
                replacement['token'] = replacement_token
                replacement['entry'] = replacement_entry
            return result
        monkeypatch.setattr(approval_context, '_fire_approval_hook', _replace_owner_on_pre_hook)
        try:
            result = approval_gateway_wait._await_gateway_decision(session_key, old_cb, {'command': 'OLD dangerous command', 'pattern_key': 'old-danger', 'pattern_keys': ['old-danger'], 'description': 'old approval prompt'}, owner_token=old_token)
            assert result['notify_failed'] is True
            assert result['stale_owner'] is True
            assert old_notified == []
            assert replacement_notified == []
            assert approval._gateway_queues[session_key] == [replacement['entry']]
            assert approval.resolve_gateway_approval(session_key, 'once') == 1
            assert replacement['entry'].result == 'once'
        finally:
            approval.clear_session(session_key)
            if 'token' in replacement:
                approval.unregister_gateway_notify(session_key, replacement['token'])

    def test_mcp_elicitation_cleanup_cannot_steal_replacement_approval(self, monkeypatch):
        from tools import approval
        session_key = 'replacement-mcp-elicitation-session'
        old_notified = threading.Event()
        replacement_notified = threading.Event()
        old_done = threading.Event()
        replacement_done = threading.Event()
        results = {}
        monkeypatch.setenv('HERMES_GATEWAY_SESSION', '1')
        monkeypatch.setattr(approval_context, '_get_approval_timeout', lambda: 5.0)

        def _request(name, done):
            token = approval_context.set_current_session_key(session_key)
            try:
                results[name] = approval_prompt.request_elicitation_consent(f'{name} request', f'{name} description')
            finally:
                approval_context.reset_current_session_key(token)
                done.set()
        old_token = approval.register_gateway_notify(session_key, lambda _data: old_notified.set())
        old_thread = threading.Thread(target=_request, args=('old', old_done), daemon=True)
        replacement_thread = None
        replacement_token = None
        try:
            old_thread.start()
            assert old_notified.wait(timeout=3.0)
            replacement_token = approval.register_gateway_notify(session_key, lambda _data: replacement_notified.set())
            approval.unregister_gateway_notify(session_key, old_token)
            replacement_thread = threading.Thread(target=_request, args=('replacement', replacement_done), daemon=True)
            replacement_thread.start()
            assert replacement_notified.wait(timeout=3.0)
            assert approval.resolve_gateway_approval(session_key, 'once') == 1
            assert replacement_done.wait(timeout=3.0)
            assert old_done.wait(timeout=3.0)
            assert results == {'old': 'decline', 'replacement': 'accept'}
        finally:
            approval.clear_session(session_key)
            approval.unregister_gateway_notify(session_key, replacement_token)
            old_thread.join(timeout=3.0)
            if replacement_thread is not None:
                replacement_thread.join(timeout=3.0)

    def test_mcp_elicitation_rejects_owner_unregistered_before_enqueue(self, monkeypatch):
        from tools import approval
        session_key = 'stale-before-enqueue-mcp-session'
        old_snapshot_taken = threading.Event()
        release_old_enqueue = threading.Event()
        replacement_notified = threading.Event()
        old_done = threading.Event()
        replacement_done = threading.Event()
        results = {}
        monkeypatch.setenv('HERMES_GATEWAY_SESSION', '1')
        monkeypatch.setattr(approval_context, '_get_approval_timeout', lambda: 5.0)
        original_await = approval_gateway_wait._await_gateway_decision
        old_token = approval.register_gateway_notify(session_key, lambda _data: None)

        def _await_after_snapshot(key, notify_cb, approval_data, *, surface='gateway', owner_token=None):
            if owner_token is old_token:
                old_snapshot_taken.set()
                assert release_old_enqueue.wait(timeout=3.0)
            return original_await(key, notify_cb, approval_data, surface=surface, owner_token=owner_token)
        monkeypatch.setattr(approval_gateway_wait, '_await_gateway_decision', _await_after_snapshot)

        def _request(name, done):
            token = approval_context.set_current_session_key(session_key)
            try:
                results[name] = approval_prompt.request_elicitation_consent(f'{name} request', f'{name} description')
            finally:
                approval_context.reset_current_session_key(token)
                done.set()
        old_thread = threading.Thread(target=_request, args=('old', old_done), daemon=True)
        replacement_thread = None
        replacement_token = None
        try:
            old_thread.start()
            assert old_snapshot_taken.wait(timeout=3.0)
            approval.unregister_gateway_notify(session_key, old_token)
            replacement_token = approval.register_gateway_notify(session_key, lambda _data: replacement_notified.set())
            release_old_enqueue.set()
            assert old_done.wait(timeout=3.0)
            assert results['old'] == 'decline'
            replacement_thread = threading.Thread(target=_request, args=('replacement', replacement_done), daemon=True)
            replacement_thread.start()
            assert replacement_notified.wait(timeout=3.0)
            assert approval.resolve_gateway_approval(session_key, 'once') == 1
            assert replacement_done.wait(timeout=3.0)
            assert results == {'old': 'decline', 'replacement': 'accept'}
        finally:
            release_old_enqueue.set()
            approval.clear_session(session_key)
            approval.unregister_gateway_notify(session_key, replacement_token)
            old_thread.join(timeout=3.0)
            if replacement_thread is not None:
                replacement_thread.join(timeout=3.0)
