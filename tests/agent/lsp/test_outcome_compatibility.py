"""Regressions found by independent A1 review: legacy callers and denied moves."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from agent.lsp import shutdown_service
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.file_operations_common import WriteResult, PatchResult

@pytest.fixture
def ops(tmp_path, monkeypatch):
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'config.yaml').write_text(json.dumps({'lsp': {'enabled': False}}))
    monkeypatch.setenv('HERMES_HOME', str(profile))
    monkeypatch.setenv('TERMINAL_CWD', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    shutdown_service()
    yield ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    shutdown_service()


def test_move_denial_keeps_original_error(ops, tmp_path, monkeypatch):
    permitted = tmp_path / 'allowed'
    permitted.mkdir()
    monkeypatch.setenv('HERMES_WRITE_SAFE_ROOT', str(permitted))
    src = tmp_path / 'untouched.txt'
    src.write_text('original')
    result = ops.move_file(str(src), str(permitted / 'target.txt'))
    assert result.error and 'denied' in result.error
    assert src.read_text() == 'original'
    assert not (permitted / 'target.txt').exists()


def test_old_write_result_positional_error_is_still_error():
    # Public dataclass signature before this change, including warning.
    result = WriteResult(0, False, None, None, None, 'legacy failure', 'legacy warning')
    assert result.error == 'legacy failure'
    assert result.warning == 'legacy warning'
    assert result.to_dict()['error'] == 'legacy failure'


def test_old_patch_result_positional_error_is_still_error():
    result = PatchResult(False, '', [], [], [], None, None, 'legacy failure', False, None)
    assert result.error == 'legacy failure'
    assert result.to_dict()['error'] == 'legacy failure'


@pytest.mark.parametrize('error', [None, 'old backend write failure'])
def test_legacy_write_override_inherited_replace(ops, tmp_path, monkeypatch, error):
    source = tmp_path / 'example.txt'
    source.write_text('before\n')
    calls = []
    def old_write(path, content, pre_content=None):
        calls.append(path)
        if error is None:
            Path(path).write_text(content)
        return SimpleNamespace(error=error, lsp_diagnostics=None, lint=None)
    monkeypatch.setattr(ops, 'write_file', old_write)
    result = ops.patch_replace(str(source), 'before', 'after')
    assert calls == [str(source)]
    if error:
        assert error in result.error
        assert source.read_text() == 'before\n'
    else:
        assert result.success and source.read_text() == 'after\n'
        assert result.lsp_verification['files'][0]['reason'] == 'legacy_provider'

