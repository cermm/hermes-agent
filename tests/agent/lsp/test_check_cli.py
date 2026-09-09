"""Explicit checks use the actual CLI and real source-reading stdio peer."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]



def invoke(project, profile, *paths):
    env = dict(os.environ, HERMES_HOME=str(profile), PYTHONPATH=str(REPO))
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "lsp", "check", "--json", *map(str, paths)],
        cwd=project, env=env, text=True, capture_output=True, timeout=45)


@pytest.mark.linux_only
def test_actual_cli_external_edit_full_outcome_and_read_only_sync(check_project):
    project, profile, wire = check_project
    source = project / "external.ts"
    source.write_text("const value = 'bad';\n")
    before = (source.read_bytes(), source.stat().st_mode, source.stat().st_mtime_ns)
    result = invoke(project, profile, source, source)
    assert result.returncode == 1, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    row = payload["files"][0]
    assert len(payload["files"]) == 1 and payload["duplicates"] == 1
    assert row["status"] == "fresh" and row["total"]["error"] == 1
    assert row["baseline"] == "not_requested" and row["delta"] is None
    assert row["source"]["text_sha256"] == hashlib.sha256(before[0]).hexdigest()
    assert "Current LSP diagnostics" in row["lsp_diagnostics"] and "baseline unavailable" not in row["lsp_diagnostics"]
    assert (source.read_bytes(), source.stat().st_mode, source.stat().st_mtime_ns) == before
    messages = [json.loads(line) for line in wire.read_text().splitlines()]
    sent = [row["message"] for row in messages if row["direction"] == "in"]
    opened = [m for m in sent if m.get("method") == "textDocument/didOpen"]
    changed = [m for m in sent if m.get("method") == "textDocument/didChange"]
    assert len(opened) == 1 and changed[0]["params"]["textDocument"]["version"] == 1
    assert opened[0]["params"]["textDocument"]["text"] == changed[0]["params"]["contentChanges"][0]["text"]
    assert not any(m.get("method") == "textDocument/didSave" for m in sent)
    assert len([m for m in sent if m.get("method") == "textDocument/diagnostic"]) <= 1
    source.write_text("const value = 1;\n")
    repaired = source.read_bytes()
    result = invoke(project, profile, source)
    assert result.returncode == 0, (result.stdout, result.stderr)
    row = json.loads(result.stdout)["files"][0]
    assert row["status"] == "fresh" and row["reason"] == "clean" and row["total"]["count"] == 0
    assert row["source"]["text_sha256"] == hashlib.sha256(repaired).hexdigest()
    assert source.read_bytes() == repaired


@pytest.mark.linux_only
@pytest.mark.parametrize("kind,reason", [("escape", "outside_root"), ("symlink", "outside_root"),
    ("nested", "outside_root"), ("directory", "not_regular_file"), ("fifo", "not_regular_file"),
    ("oversize", "file_too_large"), ("aggregate", "total_bytes_limit"),
    ("count", "file_count_limit"), ("encoding", "invalid_encoding"), ("fakegit", "invalid_root")])
def test_invalid_batch_never_starts_configured_peer(check_project, kind, reason):
    project, profile, wire = check_project
    good = project / "good.ts"
    good.write_text("const value = 1;\n")
    bad = project / "bad.ts"
    bad.write_text("bad\n")
    def created(path, content=b"bad\n"):
        path.write_bytes(content)
        return path
    def replaced(action):
        bad.unlink()
        action(bad)
        return [good, bad]
    def nested():
        directory = project / "nested"
        subprocess.run(["git", "init", "-q", str(directory)], check=True)
        return [good, created(directory / "bad.ts")]
    def fakegit():
        fake = project.parent / "fake"
        (fake / ".git").mkdir(parents=True)
        return ["--root", fake, good]
    cases = {
        "escape": lambda: [good, created(project.parent / "outside.ts")],
        "symlink": lambda: replaced(lambda path: path.symlink_to(created(project.parent / "outside.ts"))),
        "nested": nested,
        "directory": lambda: [good, project],
        "fifo": lambda: replaced(os.mkfifo),
        "oversize": lambda: [good, created(bad, b"x" * (1024 * 1024 + 1))],
        "aggregate": lambda: [created(project / f"{index}.ts", b"x" * (1024 * 1024)) for index in range(9)],
        "count": lambda: [good] * 17,
        "encoding": lambda: [good, created(bad, b"\xff\xfe")],
        "fakegit": fakegit,
    }
    paths = cases[kind]()
    result = invoke(project, profile, *paths)
    assert result.returncode == 2, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["files"] and all(row["reason"] == reason for row in payload["files"])
    assert not wire.exists(), "invalid batch started the configured language server"
    assert not (profile / "lsp").exists(), "invalid batch created managed dependency state"


@pytest.mark.linux_only
def test_aliases_missing_paths_and_git_environment(check_project, monkeypatch):
    project, profile, wire = check_project
    source = project / "actual.ts"
    source.write_text("const value = 1;\n")
    alias = project / "alias.ts"
    alias.symlink_to(source)
    old = project / "deleted.ts"
    old.write_text("old\n")
    old.unlink()
    other = project.parent / "unrelated"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    result = invoke(project, profile, source, alias, old)
    assert result.returncode == 2, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["context"]["root"] == str(project)
    assert payload["duplicates"] == 1
    assert [(row["status"], row["reason"]) for row in payload["files"]] == [
        ("fresh", "clean"), ("not_checked", "missing_file")]
    assert source.read_text() == "const value = 1;\n" and not old.exists()


@pytest.mark.linux_only
@pytest.mark.parametrize("stage", ["initialize", "diagnostics"])
def test_actual_cli_interrupt_reaps_owned_peer(check_project, stage):
    import signal
    import time
    import psutil
    project, profile, wire = check_project
    config = json.loads((profile / "config.yaml").read_text())
    config["lsp"]["servers"]["typescript"]["env"]["CHECK_STALL"] = stage
    (profile / "config.yaml").write_text(json.dumps(config))
    source = project / "cancel.ts"
    source.write_text("bad\n")
    before = source.read_bytes()
    env = dict(os.environ, HERMES_HOME=str(profile), PYTHONPATH=str(REPO))
    proc = subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "lsp", "check", "--json", str(source)],
        cwd=project, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child = None
    try:
        wanted = "initialize" if stage == "initialize" else "textDocument/didChange"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = [json.loads(line) for line in wire.read_text().splitlines() if line] if wire.exists() else []
            seen = next((row for row in rows if row["direction"] == "in" and row["message"].get("method") == wanted), None)
            if seen:
                child = psutil.Process(seen["pid"])
                child_start = child.create_time()
                break
            assert proc.poll() is None, proc.communicate()
            time.sleep(.02)
        assert child is not None, "peer did not reach the gated stage"
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 130, (stdout, stderr)
        result = json.loads(stdout)
        assert result["exit_code"] == 130 and result["files"][0]["reason"] == "cancelled"
        assert source.read_bytes() == before
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE or child.create_time() != child_start
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        if child is not None and child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            child.kill()


@pytest.mark.linux_only
@pytest.mark.parametrize("setting", ["disabled", "missing", "invalid-config"])
def test_disabled_or_unavailable_backend_never_claims_clean(check_project, setting):
    project, profile, wire = check_project
    cfg = json.loads((profile / "config.yaml").read_text())
    if setting == "disabled":
        cfg["lsp"]["enabled"] = False
    elif setting == "missing":
        cfg["lsp"]["servers"]["typescript"]["command"] = [str(project / "missing-language-server")]
    else:
        cfg["lsp"]["wait_timeout"] = "invalid"
    (profile / "config.yaml").write_text(json.dumps(cfg))
    source = project / "unchecked.ts"
    source.write_text("const value = 1;\n")
    result = invoke(project, profile, source)
    assert result.returncode == 2, (result.stdout, result.stderr)
    row = json.loads(result.stdout)["files"][0]
    assert row["status"] in ("no_verdict", "not_checked")
    assert row["total"] is None and row["delta"] is None and "source" not in row
    assert not wire.exists() and not (profile / "lsp").exists()
