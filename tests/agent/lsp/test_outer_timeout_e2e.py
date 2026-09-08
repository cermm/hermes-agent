"""Issue39 real stdio red: a successful initialized client must survive baseline timeout."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from agent.lsp import manager, servers, shutdown_service, get_service
from tools import file_tools
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

EVIDENCE = Path(__file__).parent

def test_initialized_client_survives_baseline_outer_timeout(tmp_path, monkeypatch, record_property):
    output = tmp_path / "receipt"
    output.mkdir(exist_ok=False)
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "tsconfig.json").write_text('{"compilerOptions":{"strict":true,"noEmit":true}}')
    source = project / "example.ts"
    good = "export const value: number = 1;\n"
    bad = 'export const value: number = "bad";\n'
    source.write_text(good)
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(json.dumps({"lsp": {
        "enabled": True, "install_strategy": "off", "wait_mode": "document", "wait_timeout": 5}}))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    monkeypatch.chdir(project)
    peer = EVIDENCE / "_slow_initialized_lsp_server.py"
    peer_log = tmp_path / "peer.jsonl"
    definition = servers.ServerDef("typescript", (".ts",), lambda path, root: root,
        lambda root, ctx: servers.SpawnSpec([sys.executable, str(peer), "--log", str(peer_log)], root, root),
        seed_first_push=True)
    monkeypatch.setattr(servers, "SERVERS", [definition])
    shutdown_service()
    ops = ShellFileOperations(LocalEnvironment(cwd=str(project)))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    entries = []
    original = manager.LSPService._query_outcome_async
    async def query(service, path, **kwargs):
        row = {"source": path, "kwargs": kwargs, "entry_ns": time.monotonic_ns()}
        entries.append(row)
        try:
            result = await original(service, path, **kwargs)
            row["return_ns"] = time.monotonic_ns()
            row["outcome"] = dataclasses.asdict(result)
            return result
        except BaseException as exc:
            row["raise_ns"] = time.monotonic_ns()
            row["exception"] = type(exc).__name__
            raise
        finally:
            row["finally_ns"] = time.monotonic_ns()
    monkeypatch.setattr(manager.LSPService, "_query_outcome_async", query)
    report = {"source_import": manager.__file__, "interpreter": sys.executable,
              "query_events": entries, "writes": []}
    try:
        for label, text in (("error", bad), ("repair", good)):
            started = time.monotonic_ns()
            result = json.loads(file_tools.write_file_tool(str(source), text, task_id="issue39-controlled"))
            report["writes"].append({"label": label, "start_ns": started, "end_ns": time.monotonic_ns(),
                "requested_sha256": hashlib.sha256(text.encode()).hexdigest(), "result": result,
                "state": get_service().get_status()})
            assert source.read_text() == text
    finally:
        shutdown_service()
        traffic = [json.loads(line) for line in peer_log.read_text().splitlines()] if peer_log.exists() else []
        report["traffic"] = traffic
        pids = {row["pid"] for row in traffic}
        report["remaining_peer_pids"] = [pid for pid in pids if psutil.pid_exists(pid)]
        data = (json.dumps(report, indent=2, default=str) + "\n").encode()
        json.loads(data)
        native = tmp_path / "receipt.json"
        native.write_bytes(data)
        assert native.read_bytes() == data
        (output / "report.json").write_bytes(data)
        assert (output / "report.json").read_bytes() == data
        record_property("receipt", data.decode())
    initializes = [r for r in traffic if r["direction"] == "in" and r["message"].get("method") == "initialize"]
    assert len(initializes) == 1
    init_id = initializes[0]["message"]["id"]
    reply = next(r for r in traffic if r["direction"] == "out" and r["message"].get("id") == init_id)
    assert "result" in reply["message"] and (reply["ns"] - initializes[0]["ns"])/1e9 >= 3.2
    assert any(q.get("exception") == "CancelledError" and q["kwargs"].get("snapshot") for q in entries)
    first = report["writes"][0]["result"]["lsp_verification"]["files"][0]
    repair = report["writes"][1]["result"]["lsp_verification"]["files"][0]
    assert first["status"] == "fresh", report["writes"]
    assert first["baseline"] == "unavailable" and first["baseline_reason"] == "timeout" and first["delta"] is None
    assert first["total"]["error"] == 1
    assert repair["status"] == "fresh" and repair["total"]["count"] == 0
    assert not report["writes"][0]["state"]["broken"]
    assert not report["remaining_peer_pids"]


def _real_service(tmp_path, monkeypatch, *, delay=0.01, release_gate=False, exit_on_open=False):
    project = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "tsconfig.json").write_text("{}")
    source = project / "example.ts"
    source.write_text("export const value: number = 1;\n")
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    monkeypatch.chdir(project)
    log = tmp_path / "peer.jsonl"
    gate = tmp_path / "publish.allowed"
    command = [sys.executable, str(EVIDENCE / "_slow_initialized_lsp_server.py"),
               "--log", str(log), "--delay", str(delay)]
    if release_gate:
        command += ["--release-file", str(gate)]
    if exit_on_open:
        command += ["--exit-on-open"]
    definition = servers.ServerDef("typescript", (".ts",), lambda path, root: root,
        lambda root, ctx: servers.SpawnSpec(command, root, root), seed_first_push=True)
    monkeypatch.setattr(servers, "SERVERS", [definition])
    service = manager.LSPService(enabled=True, install_strategy="off", wait_mode="document", wait_timeout=5)
    return service, source, log, gate


@pytest.mark.parametrize("read_only", [False, True])
def test_current_outer_timeout_preserves_same_connection(tmp_path, monkeypatch, record_property, read_only):
    service, source, log, gate = _real_service(tmp_path, monkeypatch, release_gate=True)
    good = source.read_text()
    bad = 'export const value: number = "bad";\n'
    report = {"read_only": read_only}
    try:
        # Explicitly initialize only. No diagnostic query or file write is retried.
        client = service._loop.run(service._get_or_spawn(str(source)), timeout=5)
        original_process = client._proc
        started = time.monotonic()
        timed = service.get_diagnostic_outcome_sync(str(source), delta=False, timeout=0.2,
            read_only=read_only, post_content=good if read_only else None)
        report.update(timeout_seconds=time.monotonic()-started, timeout_outcome=dataclasses.asdict(timed),
                      after_timeout=service.get_status())
        assert timed.status == "no_verdict" and timed.reason == "timeout"
        assert report["timeout_seconds"] < 1
        assert client.is_running and client._proc is original_process
        assert service.enabled_for(str(source)) and not service.get_status()["broken"]
        gate.touch()
        for label, text in (("error", bad), ("repair", good)):
            if not read_only:
                source.write_text(text)
            result = service.get_diagnostic_outcome_sync(str(source), delta=False,
                read_only=read_only, post_content=text if read_only else None)
            report[label] = dataclasses.asdict(result)
            assert result.status == "fresh" and result.text == text
            assert len(result.diagnostics) == (label == "error")
            assert result.baseline == "not_requested" and result.delta is None
            assert client._proc is original_process
        assert source.read_text() == good
    finally:
        service.shutdown()
        traffic = [json.loads(line) for line in log.read_text().splitlines()]
        report["traffic"] = traffic
        record_property("receipt", json.dumps(report, default=str))
    assert sum(row["direction"] == "in" and row["message"].get("method") == "initialize" for row in traffic) == 1
    assert sum(row["direction"] == "in" and row["message"].get("method") == "textDocument/diagnostic" for row in traffic) == 1
    assert not psutil.pid_exists(traffic[0]["pid"])


def test_outer_timeout_during_startup_keeps_broken_cleanup(tmp_path, monkeypatch, record_property):
    service, source, log, gate = _real_service(tmp_path, monkeypatch, delay=5)
    try:
        result = service.get_diagnostic_outcome_sync(str(source), delta=False, timeout=0.2)
        assert result.status == "no_verdict" and result.reason == "timeout"
        assert not service.enabled_for(str(source))
        started = time.monotonic()
        again = service.get_diagnostic_outcome_sync(str(source), delta=False)
        assert again.status == "no_verdict" and time.monotonic()-started < 0.5
        assert not service.get_status()["clients"]
    finally:
        service.shutdown()
    traffic = [json.loads(line) for line in log.read_text().splitlines()]
    record_property("traffic", json.dumps(traffic))
    assert len([row for row in traffic if row["message"].get("method") == "initialize"]) == 1
    assert not psutil.pid_exists(traffic[0]["pid"])
    assert not service._starting and not service._spawning


@pytest.mark.parametrize("closed", [False, True])
def test_failure_handling_does_not_preserve_invalid_connections(tmp_path, monkeypatch, closed):
    service, source, log, gate = _real_service(tmp_path, monkeypatch, exit_on_open=closed)
    try:
        client = service._loop.run(service._get_or_spawn(str(source)), timeout=5)
        process = client._proc
        if closed:
            result = service.get_diagnostic_outcome_sync(str(source), delta=False)
            assert result.status == "no_verdict" and not client.is_running
            error = TimeoutError("outer deadline with an observed closed transport")
        else:
            assert client.is_running
            error = RuntimeError("non-timeout operation failure")
        service._mark_broken_for_file(str(source), error)
        assert not service.enabled_for(str(source))
        assert not service.get_status()["clients"]
    finally:
        service.shutdown()
    assert not psutil.pid_exists(process.pid)
