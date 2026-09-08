"""Disposable board -> actual subprocess -> production CLI -> real MCP handlers."""
import hashlib
import json
import os
from pathlib import Path
import sys

import psutil

from tests.tools.test_mcp_project_binding import projects, config, git


def test_real_dispatch_preserves_roles_and_binds_each_worker(projects, monkeypatch):
    a, b, home = projects
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setenv("TERMINAL_CWD", str(home))  # stale parent must be replaced
    (home / "config.yaml").write_text("{}")
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    kb.create_board(slug="default", name="Disposable routing proof")
    configs = {}
    for role, root, other, model, denied in (("builder", a, b, "gpt-5.6-luna", ["web"]),
                                            ("reviewer", b, a, "gpt-6-astra", ["web", "terminal"])):
        profile = home / "profiles" / role
        profile.mkdir(parents=True)
        server = "navigation_" + role
        configs[role] = {"model": {"default": model, "provider": "openai-codex"},
            "platform_toolsets": {"cli": ["file", "terminal", "web", server]},
            "agent": {"disabled_toolsets": denied}, "tools": {"tool_search": {"enabled": "off"}},
            "mcp_servers": {server: config()}, "routing_fixture": {"server": server, "other": str(other)}}
        (profile / "config.yaml").write_text(json.dumps(configs[role]))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.glob("profiles/*/config.yaml")}
    probe = Path(__file__).with_name("_project_worker_probe.py")
    monkeypatch.setattr(dispatch, "_resolve_hermes_argv", lambda: [sys.executable, str(probe)])
    with kbc.connect_closing() as conn:
        ids = [kb.create_task(conn, title="Disposable routing", assignee=role, workspace_kind="dir",
                              workspace_path=str(root), model_override=model, provider_override="openai-codex",
                              reasoning_effort=effort)
               for role, root, model, effort in (("builder", a, "gpt-5.6-luna", "low"),
                                                  ("reviewer", b, "gpt-6-astra", "high"))]
        result = dispatch.dispatch_once(conn, max_spawn=2, reconcile_orphans=False)
    assert len(result.spawned) == 2, result
    receipts = []
    for role, root, identity, effort in (("builder", a, "alpha", "low"), ("reviewer", b, "beta", "high")):
        profile = home / "profiles" / role
        receipt_file = profile / "routing-receipt.json"
        import time
        deadline = time.monotonic() + 40
        while not receipt_file.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert receipt_file.exists(), [p.read_text() for p in home.rglob("*.log")]
        receipt = json.loads(receipt_file.read_text())
        receipts.append(receipt)
        assert receipt["outcome"] == "PASS", receipt
        try:
            process = psutil.Process(receipt["pid"])
            assert process.create_time() == receipt["created"]
            process.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass  # Popen's child reaper may already have collected this worker.
        assert receipt["cwd"] == receipt["effective_cwd"] == str(root)
        assert receipt["env"]["TERMINAL_CWD"] == str(root)
        assert receipt["env"]["HERMES_HOME"] == str(profile)
        assert receipt["env"]["HERMES_PROFILE"] == role
        assert receipt["parsed_model"] == configs[role]["model"]["default"]
        assert receipt["parsed_provider"] == "openai-codex" and receipt["parsed_reasoning"] == effort
        assert receipt["model_config"] == configs[role]["model"]
        assert receipt["denied"] == configs[role]["agent"]["disabled_toolsets"]
        assert json.loads(receipt["result"]["result"])["source"] == identity
        assert receipt["result"]["project_identity"] == {
            "root": str(root), "commit": git(root, "rev-parse", "HEAD"), "mode": "workspace", "matches_task": True}
        assert receipt["binding"]["profile"] == str(profile)
        assert receipt["cleanup"]["remaining"] == []
        assert all(s["cwd"] == s["effective_cwd"] == str(root) for s in receipt["startup"])
        assert all(configs[role]["routing_fixture"]["server"] in s["filter"] for s in receipt["startup"])
    assert len({r["pid"] for r in receipts}) == 2
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
    print("Fresh worker receipts", json.dumps(receipts))
