"""Deferred commands and replacement gateway runs retain explicit consent ownership."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tools import approval, approval_context, approval_gateway_wait


@pytest.fixture(autouse=True)
def isolated_approval_state(monkeypatch):
    for name in ('_gateway_notify_cbs', '_gateway_notify_tokens', '_gateway_queues',
                 '_session_approved', '_pending'):
        monkeypatch.setattr(approval, name, {}, raising=False)
    monkeypatch.setattr(approval, '_permanent_approved', set())
    monkeypatch.setattr(approval, '_session_yolo', set())
    monkeypatch.setattr(approval, '_tirith_scan', lambda command: {'action': 'allow'})
    monkeypatch.setattr(approval_context, '_fire_approval_hook', lambda *a, **kw: None)
    monkeypatch.setattr(approval_context, '_get_approval_timeout', lambda: 2)


@pytest.mark.parametrize('choice', ['once', 'always', 'session'])
@pytest.mark.parametrize('env_type,mode', [('local', 'off'), ('docker', 'smart')])
def test_deferred_command_requires_exact_session_choice(monkeypatch, choice, env_type, mode):
    monkeypatch.setattr(approval, '_presence', lambda cb=None: (cb, True, False, False))
    monkeypatch.setattr(approval_context, '_get_approval_mode', lambda: mode)
    monkeypatch.setattr(approval, '_yolo_active', lambda: True)
    monkeypatch.setattr(approval, '_command_matches_permanent_allowlist', lambda cmd: True)
    monkeypatch.setattr(approval, '_present_with_selected_transport', lambda **kw: None)
    monkeypatch.setattr(approval, '_transport_choice', lambda *a, **kw: (None, None))
    prompts = []
    def prompt(command, description, **kwargs):
        assert kwargs['allow_permanent'] is False
        prompts.append(command)
        return choice
    monkeypatch.setattr(approval, 'prompt_dangerous_approval', prompt)
    token = approval_context.set_current_session_key('deferred-owner')
    try:
        def check(command):
            return approval.check_all_command_guards(command, env_type, require_explicit_authorization=True)
        result = check('printf first')
        assert result['approved'] is (choice == 'session')
        if choice == 'session':
            assert check('printf first')['approved'] is True
            assert prompts == ['printf first']
            assert check('printf second')['approved'] is True
            assert prompts == ['printf first', 'printf second']
            assert approval._permanent_approved == set()
        else:
            assert result['outcome'] == 'session_authorization_required'
            assert approval._session_approved == {}
    finally:
        approval_context.reset_current_session_key(token)


def test_old_registration_cannot_remove_replacement_or_replay_pending_request(monkeypatch):
    old_cb = lambda data: None
    new_cb = lambda data: None
    old_token = approval.register_gateway_notify('session', old_cb)
    new_token = approval.register_gateway_notify('session', new_cb)
    approval.unregister_gateway_notify('session', old_token)
    assert approval._gateway_notify_cbs['session'] is new_cb
    assert approval._gateway_notify_tokens['session'] is new_token
    result = approval_gateway_wait._await_gateway_decision(
        'session', old_cb, {'command': 'old'}, owner_token=old_token,
    )
    assert result['stale_owner'] is True
    assert approval.list_gateway_approvals('session') == []


def test_replacement_requests_do_not_coalesce_with_old_generation():
    notified_old, notified_new = threading.Event(), threading.Event()
    def old_cb(data):
        notified_old.set()
    def new_cb(data):
        notified_new.set()
    data = {'command': 'same command', 'pattern_keys': ['same']}
    with ThreadPoolExecutor(max_workers=2) as workers:
        old_token = approval.register_gateway_notify('session', old_cb)
        old = workers.submit(approval_gateway_wait._await_gateway_decision,
                             'session', old_cb, data, owner_token=old_token)
        assert notified_old.wait(2)
        new_token = approval.register_gateway_notify('session', new_cb)
        new = workers.submit(approval_gateway_wait._await_gateway_decision,
                             'session', new_cb, data, owner_token=new_token)
        assert notified_new.wait(2), 'new run adopted stale pending approval'
        approval.unregister_gateway_notify('session', old_token)
        pending = approval.list_gateway_approvals('session')
        assert len(pending) == 1
        approval.resolve_gateway_approval('session', 'session', request_id=pending[0]['request_id'])
        assert old.result(timeout=2)['choice'] is None
        assert new.result(timeout=2)['choice'] == 'session'
        approval.unregister_gateway_notify('session', new_token)
