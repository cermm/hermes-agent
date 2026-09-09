"""Full-current checks preserve project changes, drift and bounded attribution."""
import json
from pathlib import Path
import subprocess
import time

import pytest

from agent.lsp import check
from agent.lsp.manager import LSPService
from agent.lsp.outcome import DiagnosticOutcome


def _activate(monkeypatch, project, profile):
    monkeypatch.chdir(project)
    monkeypatch.setenv("HERMES_HOME", str(profile))


@pytest.mark.linux_only
def test_later_explicit_query_rechecks_unchanged_text_after_dependency_edit(check_project, monkeypatch):
    project, profile, wire = check_project
    _activate(monkeypatch, project, profile)
    source = project / "target.ts"
    text = "const value = 1;\n"
    source.write_text(text)
    dependency = project / "dependency.txt"
    dependency.write_text("good")
    cfg = json.loads((profile / "config.yaml").read_text())
    cfg["lsp"]["servers"]["typescript"]["env"]["CHECK_DEPENDENCY"] = str(dependency)
    (profile / "config.yaml").write_text(json.dumps(cfg))
    service = LSPService.create_from_config(install_strategy_override="manual")
    before = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)
    versions = []
    try:
        for contents, errors in [("good", 0), ("bad", 1), ("good", 0)]:
            dependency.write_text(contents)
            result = service.get_diagnostic_outcome_sync(str(source), delta=False, post_content=text, read_only=True)
            _, row = result.as_file(str(source), "check")
            assert row["status"] == "fresh" and row["total"]["error"] == errors
            assert row["baseline"] == "not_requested" and row["delta"] is None
            versions.append(row["source"]["document_version"])
        assert versions[0] < versions[1] < versions[2]
        assert (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode) == before
    finally:
        service.shutdown()


@pytest.mark.linux_only
@pytest.mark.parametrize("change,reason", [("source", "source_changed"), ("alias", "source_changed"), ("head", "context_changed")])
def test_after_query_drift_invalidates_fresh_attribution(check_project, monkeypatch, change, reason):
    project, profile, wire = check_project
    _activate(monkeypatch, project, profile)
    source = project / "target.ts"
    source.write_text("const value = 1;\n")
    alias = project / "alias.ts"
    alias.symlink_to(source)
    original = LSPService.get_diagnostic_outcome_sync
    def changed(service, *args, **kwargs):
        outcome = original(service, *args, **kwargs)
        assert outcome.status == "fresh"
        if change == "source":
            source.write_text("const value = 'bad';\n")
        elif change == "alias":
            other = project / "other.ts"
            other.write_text("bad\n")
            alias.unlink()
            alias.symlink_to(other)
        else:
            subprocess.run(["git", "add", "target.ts"], cwd=project, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                            "commit", "-qm", "external context change"], cwd=project, check=True)
        return outcome
    monkeypatch.setattr(LSPService, "get_diagnostic_outcome_sync", changed)
    output, code = check.check_files([str(alias)])
    assert code == 2
    row = output["files"][0]
    assert row["status"] == "no_verdict" and row["reason"] == reason
    assert row["total"] is None and row["delta"] is None and "source" not in row


def test_escaped_complete_envelope_is_bounded_and_omission_is_not_success():
    diagnostic = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                  "severity": 1, "message": "λ" * 1200, "code": "CHECK001"}
    records = []
    for index in range(check.MAX_FILES):
        text, row = DiagnosticOutcome("fresh", "diagnostics_present", diagnostics=[diagnostic],
            server_id="test", document_version=1, text="unchanged").as_file(f"/project/{index}.ts", "check")
        row["lsp_diagnostics"] = text
        records.append(row)
    output, code = check._result(records, (Path("/root/\x1b[31m" + "λ" * 600), "a" * 40), 16, 0, 1, 0)
    assert len(json.dumps(output, ensure_ascii=True)) <= check.MAX_OUTPUT_CHARS
    assert code == 2 and output["omitted_files"] > 0 and output["files"]
    assert "\x1b" not in output["context"]["root"] and output["context"]["root_truncated"]
    assert all(row["total"]["count"] == 1 for row in output["files"])


@pytest.mark.linux_only
def test_request_deadline_stops_unstarted_files_and_reaps_initialization(check_project, monkeypatch):
    import psutil
    project, profile, wire = check_project
    _activate(monkeypatch, project, profile)
    cfg = json.loads((profile / "config.yaml").read_text())
    cfg["lsp"]["servers"]["typescript"]["env"]["CHECK_STALL"] = "initialize"
    (profile / "config.yaml").write_text(json.dumps(cfg))
    paths = [project / "one.ts", project / "two.ts"]
    for path in paths:
        path.write_text("bad\n")
    monkeypatch.setattr(check, "CHECK_TIMEOUT", 1.0)
    start = time.monotonic()
    output, code = check.check_files([str(path) for path in paths])
    assert code == 2 and time.monotonic() - start < 12
    assert output["files"][0]["status"] == "no_verdict"
    assert output["files"][1]["status"] == "not_checked" and output["files"][1]["reason"] == "deadline_exceeded"
    rows = [json.loads(line) for line in wire.read_text().splitlines()]
    pid = rows[0]["pid"]
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert all(path.read_text() == "bad\n" for path in paths)
