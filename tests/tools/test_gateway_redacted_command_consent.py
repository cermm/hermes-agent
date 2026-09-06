"""Independent consent boundaries through actual gateway command guards."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from agent.redact import redact_sensitive_text
from tools import approval, approval_context


@pytest.fixture(autouse=True)
def private_state(monkeypatch):
    for name in ("_gateway_notify_cbs", "_gateway_notify_tokens", "_gateway_queues", "_session_approved", "_pending"):
        monkeypatch.setattr(approval, name, {})
    monkeypatch.setattr(approval, "_permanent_approved", set())
    monkeypatch.setattr(approval, "_session_yolo", set())
    monkeypatch.setattr(approval, "_tirith_scan", lambda _: {"action": "allow"})
    monkeypatch.setattr(approval, "_presence", lambda cb=None: (cb, False, True, False))
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 5)
    monkeypatch.setattr(approval, "_present_with_selected_transport", lambda **kw: None)
    monkeypatch.setattr(approval, "_transport_choice", lambda *a, **kw: (None, None))


def test_redacted_commands_cannot_share_exact_deferred_session_consent(monkeypatch):
    # Synthetic token-shaped path names: no real secret and no command is executed.
    first = "rm -rf /tmp/sk-fixtureAAAAAAfinish"
    second = "rm -rf /tmp/sk-fixtureBBBBBBfinish"
    assert first != second and redact_sensitive_text(first) == redact_sensitive_text(second)
    notified, reached = threading.Event(), threading.Event()
    prompts = []

    def hook(name, **payload):
        if name == "pre_approval_request" and payload.get("coalesced"):
            reached.set()

    def notify(payload):
        prompts.append(payload)
        notified.set()
        if len(prompts) > 1:
            reached.set()

    monkeypatch.setattr(approval_context, "_fire_approval_hook", hook)
    owner = approval.register_gateway_notify("same-session", notify)

    def guard(command):
        token = approval_context.set_current_session_key("same-session")
        try:
            return approval.check_all_command_guards(command, "local", require_explicit_authorization=True)
        finally:
            approval_context.reset_current_session_key(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            one = pool.submit(guard, first)
            assert notified.wait(5)
            two = pool.submit(guard, second)
            assert reached.wait(5)
            approval.resolve_gateway_approval("same-session", "session", request_id=prompts[0]["request_id"])
            if len(prompts) > 1:
                approval.resolve_gateway_approval("same-session", "deny", request_id=prompts[1]["request_id"])
            result_one, result_two = one.result(timeout=5), two.result(timeout=5)
            assert result_one["approved"] is True
            assert result_two["approved"] is False, "consent to the first command silently authorized different raw bytes"
            assert len(prompts) == 2
            assert approval._deferred_command_session_key(second) not in approval._session_approved.get("same-session", set())
        finally:
            approval.unregister_gateway_notify("same-session", owner)


@pytest.mark.parametrize('required,choice', [(True, 'session'), (False, 'session'), (False, 'once')])
def test_identical_command_coalesces_without_sharing_once_consent(monkeypatch, required, choice):
    prompts = []
    first_prompt, coalesced, second_prompt = threading.Event(), threading.Event(), threading.Event()
    def hook(name, **payload):
        if name == 'pre_approval_request' and payload.get('coalesced'):
            coalesced.set()
    def notify(data):
        prompts.append(data)
        (first_prompt if len(prompts) == 1 else second_prompt).set()
    monkeypatch.setattr(approval_context, '_fire_approval_hook', hook)
    owner = approval.register_gateway_notify('same-session', notify)
    def guard():
        token = approval_context.set_current_session_key('same-session')
        try:
            return approval.check_all_command_guards('rm -rf /tmp/synthetic-target', 'local',
                                                     require_explicit_authorization=required)
        finally:
            approval_context.reset_current_session_key(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(guard)
            assert first_prompt.wait(5)
            second = pool.submit(guard)
            assert coalesced.wait(5)
            approval.resolve_gateway_approval('same-session', choice, request_id=prompts[0]['request_id'])
            if choice == 'once':
                assert second_prompt.wait(5)
                approval.resolve_gateway_approval('same-session', 'deny', request_id=prompts[1]['request_id'])
            assert first.result(timeout=5)['approved'] is True
            assert second.result(timeout=5)['approved'] is (choice == 'session')
            assert len(prompts) == (2 if choice == 'once' else 1)
        finally:
            approval.unregister_gateway_notify('same-session', owner)
