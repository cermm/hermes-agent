from tools import approval_context, approval_gateway_wait, approval_prompt, approval_smart
"""Tests for check_all_command_guards() — combined tirith + dangerous command guard."""
import os
from unittest.mock import patch, MagicMock
import pytest
import tools.approval as approval_module
from tools.approval import approve_permanent, approve_session, check_all_command_guards, check_dangerous_command, detect_dangerous_command, is_approved
from tools.approval_context import reset_current_session_key, reset_deferred_command_session_authorization_required, set_current_session_key, set_deferred_command_session_authorization_required
import tools.tirith_security

def _tirith_result(action='allow', findings=None, summary=''):
    return {'action': action, 'findings': findings or [], 'summary': summary}
_TIRITH_PATCH = 'tools.tirith_security.check_command_security'

@pytest.fixture(autouse=True)
def _clean_state():
    """Clear approval state and relevant env vars between tests."""
    approval_module._session_approved.clear()
    approval_module._pending.clear()
    approval_module._permanent_approved.clear()
    saved = {}
    for k in ('HERMES_INTERACTIVE', 'HERMES_GATEWAY_SESSION', 'HERMES_EXEC_ASK', 'HERMES_YOLO_MODE'):
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    yield
    approval_module._session_approved.clear()
    approval_module._pending.clear()
    approval_module._permanent_approved.clear()
    for k, v in saved.items():
        os.environ[k] = v
    for k in ('HERMES_INTERACTIVE', 'HERMES_GATEWAY_SESSION', 'HERMES_EXEC_ASK', 'HERMES_YOLO_MODE'):
        os.environ.pop(k, None)

class TestDeferredCommandAuthorization:

    @staticmethod
    def _dangerous_command_and_pattern():
        command = 'rm -rf /tmp/test'
        dangerous, pattern_key, _description = detect_dangerous_command(command)
        assert dangerous is True
        assert pattern_key
        return (command, pattern_key)

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_once_choice_does_not_authorize_deferred_gate(self, mock_tirith):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='once')
        command, _pattern_key = self._dangerous_command_and_pattern()
        result = check_all_command_guards(command, 'local', approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is False
        assert result['outcome'] == 'session_authorization_required'
        assert result['user_consent'] is False
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_session_choice_authorizes_deferred_gate(self, mock_tirith):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='session')
        command, _pattern_key = self._dangerous_command_and_pattern()
        result = check_all_command_guards(command, 'local', approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is True
        assert result['user_approved'] is True
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_existing_pattern_session_approval_does_not_authorize_deferred_gate(self, mock_tirith):
        session_key = 'deferred-session-approval'
        token = set_current_session_key(session_key)
        try:
            os.environ['HERMES_INTERACTIVE'] = '1'
            cb = MagicMock(return_value='once')
            command, pattern_key = self._dangerous_command_and_pattern()
            approve_session(session_key, pattern_key)
            result = check_all_command_guards(command, 'local', approval_callback=cb, require_explicit_authorization=True)
        finally:
            reset_current_session_key(token)
        assert result['approved'] is False
        assert result['outcome'] == 'session_authorization_required'
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_existing_exact_dangerous_command_session_approval_authorizes_deferred_gate(self, mock_tirith):
        session_key = 'deferred-exact-dangerous-command-session'
        token = set_current_session_key(session_key)
        try:
            os.environ['HERMES_INTERACTIVE'] = '1'
            cb = MagicMock(return_value='deny')
            command, _pattern_key = self._dangerous_command_and_pattern()
            approve_session(session_key, approval_module._deferred_command_session_key(command))
            result = check_all_command_guards(command, 'local', approval_callback=cb, require_explicit_authorization=True)
        finally:
            reset_current_session_key(token)
        assert result['approved'] is True
        cb.assert_not_called()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_deferred_session_approval_is_exact_command_not_pattern_scope(self, mock_tirith):
        session_key = 'deferred-exact-command-not-pattern'
        first_command = 'rm -rf /tmp/one'
        second_command = 'rm -rf /tmp/two'
        first_dangerous, first_pattern, _ = detect_dangerous_command(first_command)
        second_dangerous, second_pattern, _ = detect_dangerous_command(second_command)
        assert first_dangerous is True
        assert second_dangerous is True
        assert first_pattern == second_pattern
        token = set_current_session_key(session_key)
        try:
            os.environ['HERMES_INTERACTIVE'] = '1'
            cb1 = MagicMock(return_value='session')
            first_result = check_all_command_guards(first_command, 'local', approval_callback=cb1, require_explicit_authorization=True)
            cb2 = MagicMock(return_value='once')
            second_result = check_all_command_guards(second_command, 'local', approval_callback=cb2, require_explicit_authorization=True)
        finally:
            reset_current_session_key(token)
        assert first_result['approved'] is True
        cb1.assert_called_once()
        assert second_result['approved'] is False
        assert second_result['outcome'] == 'session_authorization_required'
        cb2.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_permanent_approval_does_not_authorize_deferred_gate(self, mock_tirith):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='once')
        command, pattern_key = self._dangerous_command_and_pattern()
        approve_permanent(pattern_key)
        result = check_all_command_guards(command, 'local', approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is False
        assert result['outcome'] == 'session_authorization_required'
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_command_allowlist_glob_does_not_authorize_deferred_gate(self, mock_tirith):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='once')
        command, _pattern_key = self._dangerous_command_and_pattern()
        approve_permanent('rm *')
        result = check_all_command_guards(command, 'local', approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is False
        assert result['outcome'] == 'session_authorization_required'
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_approval_mode_off_does_not_authorize_deferred_gate(self, mock_tirith, monkeypatch):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='once')
        command, _pattern_key = self._dangerous_command_and_pattern()
        monkeypatch.setattr(approval_context, '_get_approval_config', lambda: {'mode': 'off'})
        result = check_all_command_guards(command, 'local', approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is False
        assert result['outcome'] == 'session_authorization_required'
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_smart_auto_approval_does_not_authorize_deferred_gate(self, mock_tirith, monkeypatch):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='once')
        command, _pattern_key = self._dangerous_command_and_pattern()
        monkeypatch.setattr(approval_context, '_get_approval_config', lambda: {'mode': 'smart'})
        smart = MagicMock(return_value='approve')
        monkeypatch.setattr(approval_smart, '_smart_approve', smart)
        result = check_all_command_guards(command, 'local', approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is False
        assert result['outcome'] == 'session_authorization_required'
        smart.assert_not_called()
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_terminal_guard_context_requires_deferred_session_authorization(self, mock_tirith, monkeypatch):
        import tools.terminal_tool as terminal_tool
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='once')
        command, _pattern_key = self._dangerous_command_and_pattern()
        monkeypatch.setattr(terminal_tool, '_get_approval_callback', lambda: cb)
        token = set_deferred_command_session_authorization_required(True)
        try:
            result = terminal_tool._check_all_guards(command, 'local')
        finally:
            reset_deferred_command_session_authorization_required(token)
        assert result['approved'] is False
        assert result['outcome'] == 'session_authorization_required'
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_scanner_allowed_command_requires_deferred_session_authorization(self, mock_tirith):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='once')
        result = check_all_command_guards('echo hello', 'local', approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is False
        assert result['outcome'] == 'session_authorization_required'
        assert 'deferred goal terminal command' in result['description']
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_scanner_allowed_command_session_choice_authorizes_deferred_gate(self, mock_tirith):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='session')
        result = check_all_command_guards('echo hello', 'local', approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is True
        assert result['user_approved'] is True
        cb.assert_called_once()

    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_existing_exact_command_session_approval_authorizes_deferred_gate(self, mock_tirith):
        session_key = 'deferred-exact-command-session'
        command = 'echo hello'
        token = set_current_session_key(session_key)
        try:
            os.environ['HERMES_INTERACTIVE'] = '1'
            cb = MagicMock(return_value='deny')
            approve_session(session_key, approval_module._deferred_command_session_key(command))
            result = check_all_command_guards(command, 'local', approval_callback=cb, require_explicit_authorization=True)
        finally:
            reset_current_session_key(token)
        assert result['approved'] is True
        cb.assert_not_called()

    @pytest.mark.parametrize('env_type', ['docker', 'singularity', 'modal', 'daytona'])
    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_isolated_backend_skip_does_not_bypass_deferred_gate(self, mock_tirith, env_type):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='once')
        command, _pattern_key = self._dangerous_command_and_pattern()
        result = check_all_command_guards(command, env_type, approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is False
        assert result['outcome'] == 'session_authorization_required'
        cb.assert_called_once()

    @pytest.mark.parametrize('env_type', ['docker', 'singularity', 'modal', 'daytona'])
    @patch(_TIRITH_PATCH, return_value=_tirith_result('allow'))
    def test_isolated_backend_session_choice_authorizes_deferred_gate(self, mock_tirith, env_type):
        os.environ['HERMES_INTERACTIVE'] = '1'
        cb = MagicMock(return_value='session')
        command, _pattern_key = self._dangerous_command_and_pattern()
        result = check_all_command_guards(command, env_type, approval_callback=cb, require_explicit_authorization=True)
        assert result['approved'] is True
        assert result['user_approved'] is True
        cb.assert_called_once()
