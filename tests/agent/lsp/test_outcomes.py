"""Real child regression: empty presentation must not erase diagnostic evidence."""
import json
import subprocess
import sys
from pathlib import Path
import pytest
from agent.lsp import get_service, shutdown_service
from agent.lsp.servers import SERVERS, ServerDef, SpawnSpec
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools import file_tools

@pytest.mark.parametrize("scenario", ["clean", "errors", "stale", "warning", "unknown_baseline", "source_changed"])
def test_real_handler_outcome_distinguishes_full_delta_and_no_verdict(tmp_path, monkeypatch, scenario):
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    profile = tmp_path / "profile"
    profile.mkdir()
    (project / "pyproject.toml").write_text("")
    source = project / "example.py"
    source.write_text("x: int = 1\nmarker = 1\n")
    fixture = Path(__file__).with_name("_mock_lsp_server.py")
    (profile / "config.yaml").write_text(json.dumps({"lsp": {"install_strategy": "off", "wait_timeout": 0.2,
        "servers": {"pyright": {"command": [sys.executable, str(fixture)], "env": {"MOCK_LSP_SCRIPT": scenario}}}}}))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    monkeypatch.chdir(project)
    original = next(s for s in SERVERS if s.server_id == "pyright")
    substitute = ServerDef("pyright", original.extensions, lambda fp, ws: ws,
        lambda root, ctx: SpawnSpec([sys.executable, str(fixture)], root, root,
                                    env={"MOCK_LSP_SCRIPT": "errors" if scenario == "source_changed" else scenario}), multi_root=True)
    monkeypatch.setattr("agent.lsp.servers.SERVERS", [substitute, *[s for s in SERVERS if s is not original]])
    shutdown_service()
    ops = ShellFileOperations(LocalEnvironment(cwd=str(project)))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    if scenario == "source_changed":
        check = ops._check_lint_delta
        def concurrent_writer(*args, **kwargs):
            result = check(*args, **kwargs)
            source.write_text("another_writer = True\n")
            return result
        monkeypatch.setattr(ops, "_check_lint_delta", concurrent_writer)
    try:
        payload = json.loads(file_tools.write_file_tool(str(source), "x: int = 1\nmarker = 2\n", task_id="a1-outcome"))
        assert payload.get("error") is None
        # Confirm the real peer initialized and supplied the expected pre-edit payload.
        service = get_service()
        assert service.get_status()["clients"][0]["running"]
        client = next(iter(service._clients.values()))
        assert len(client.diagnostics_for(str(source))) == (scenario != "clean")
        outcome = payload["lsp_verification"]["files"][0]
        if scenario in {"stale", "source_changed"}:
            assert outcome["status"] == "no_verdict"
            assert outcome["total"] is None
            if scenario == "source_changed":
                assert outcome["reason"] == "source_changed"
        else:
            assert outcome["status"] == "fresh"
            assert outcome["total"]["count"] == (scenario != "clean")
            if scenario == "unknown_baseline":
                assert outcome["delta"] is None
                assert outcome["baseline"] == "unavailable"
                assert "baseline unavailable" in payload["lsp_diagnostics"]
                assert "introduced" not in payload["lsp_diagnostics"]
            else:
                assert outcome["delta"]["count"] == 0
                assert outcome["baseline"] == "available"
            if scenario == "warning":
                assert outcome["total"]["warning"] == 1
                assert "lsp_diagnostics" not in payload
            assert outcome["reason"] == ("clean" if scenario == "clean" else "diagnostics_present")
            assert outcome["source"]["document_version"] >= 1
    finally:
        shutdown_service()
