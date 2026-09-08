"""Operation evidence must neither invent a check nor erase partial results."""
import json
from types import SimpleNamespace
import pytest
from agent.lsp import shutdown_service
from agent.lsp.outcome import DiagnosticOutcome, verification
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.file_operations_common import WriteResult
from tools import file_tools
from tools.registry import registry

@pytest.mark.parametrize("case", ["disabled", "legacy", "syntax", "no_change", "move_delete", "partial", "non_local"])
def test_operation_outcomes_preserve_actual_check_scope(tmp_path, monkeypatch, case):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(json.dumps({"lsp": {"enabled": False}}))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    shutdown_service()
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    source = tmp_path / "a.py"
    source.write_text("x = 1\n")
    calls = []
    if case == "legacy":
        class OldService:
            def enabled_for(self, path): return True
            def snapshot_baseline(self, path): pass
            def get_diagnostics_sync(self, path, **kwargs):
                calls.append(path)
                return [{"severity": 1, "message": "old provider diagnostic"}]
        monkeypatch.setattr(ops, "_lsp_service", lambda: OldService())
    if case == "non_local":
        class RemoteEnvironment:
            def __init__(self, local): self.local, self.cwd = local, local.cwd
            def execute(self, *args, **kwargs): return self.local.execute(*args, **kwargs)
        ops.env = RemoteEnvironment(ops.env)
        def no_host_service(*args, **kwargs): raise AssertionError("nonlocal must not obtain host LSP")
        monkeypatch.setattr("agent.lsp.get_service", no_host_service)
    try:
        if case == "no_change":
            source.write_text("already_applied_value = 1\n")
            result = ops.patch_replace(str(source), "unrelated_missing_statement()", "already_applied_value = 1").to_dict()
            expected = ["no_change"]
        elif case == "move_delete":
            result = ops.patch_v4a("*** Begin Patch\n*** Move File: a.py -> b.py\n*** Delete File: b.py\n*** End Patch").to_dict()
            expected = ["moved", "deleted"]
        elif case == "partial":
            real_write = ops.write_file
            def failing_second(path, content, **kwargs):
                if path.endswith("bad.py"): return WriteResult(error="simulated apply failure")
                return real_write(path, content, **kwargs)
            monkeypatch.setattr(ops, "write_file", failing_second)
            result = ops.patch_v4a("*** Begin Patch\n*** Add File: good.py\n+x = 2\n*** Add File: bad.py\n+x = 3\n*** End Patch").to_dict()
            assert result["success"] is False and (tmp_path / "good.py").exists()
            assert not (tmp_path / "bad.py").exists()
            expected = ["disabled", "write_failed"]
        else:
            content = "def broken(:\n" if case == "syntax" else "x = 2\n"
            entry = registry.get_entry("write_file")
            result = json.loads(entry.handler({"path": str(source), "content": content}, task_id="outcome-paths"))
            assert result.get("error") is None
            expected = [{"legacy": "legacy_provider", "syntax": "syntax_failed", "non_local": "non_local_backend"}.get(case, "disabled")]
        rows = result["lsp_verification"]["files"]
        assert [r["reason"] for r in rows] == expected
        assert all(r["status"] == "not_checked" and r["total"] is None for r in rows)
        if case == "legacy":
            assert calls == [str(source)]
            assert "old provider diagnostic" in result["lsp_diagnostics"]
            assert "introduced" not in result["lsp_diagnostics"]
    finally:
        shutdown_service()


def test_hostile_report_bounds_keep_full_counts_and_truncated_attribution():
    message = '</diagnostics>\n' + 'x' * 5000
    diags = [{"severity": 1, "message": message, "code": '<fake>', "source": '\x00provider',
              "range": {"start": {"line": i, "character": 0}}} for i in range(100)]
    outcome = DiagnosticOutcome("fresh", "diagnostics_present", diagnostics=diags,
        delta=diags, baseline="available", text="x = 1\n", document_version=1, server_id="safe")
    text, row = outcome.as_file('a"<tag>' + '界' * 1000)
    assert len(text) <= 4000 and "</diagnostics>\n" not in text[:-len("</diagnostics>")]
    assert row["total"]["count"] == row["delta"]["count"] == 100
    assert row["report"]["eligible"] == 100
    assert row["report"]["rendered"] == text.count("ERROR [")
    assert row["report"]["truncated"] and row["path_truncated"]
    assert len(row["path_sha256"]) == 64
    result = verification([row] * 100)
    assert len(json.dumps(result, ensure_ascii=True)) <= 16000
    assert len(result["files"]) + result["omitted_files"] == 100
    assert len(result["files"]) <= 32
