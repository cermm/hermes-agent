"""Real Git worktrees, real stdio transport and registry dispatch; no live user state."""
import copy
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from agent.runtime_cwd import _SESSION_CWD, set_session_cwd
from tools import mcp_tool as core
from tools.mcp_tool_discovery import discover_mcp_tools, register_mcp_servers
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from tools.registry import registry


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def projects(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    a = tmp_path / "a"
    a.mkdir()
    git(a, "init", "-b", "main")
    git(a, "config", "user.name", "Fixture")
    git(a, "config", "user.email", "fixture@example.invalid")
    (a / "symbols.txt").write_text("alpha")
    git(a, "add", "symbols.txt")
    git(a, "commit", "-m", "alpha")
    b = tmp_path / "b"
    git(a, "worktree", "add", "-b", "second", str(b))
    (b / "symbols.txt").write_text("beta")
    git(b, "commit", "-am", "beta")
    token = set_session_cwd(str(a))
    yield a, b, home
    shutdown_mcp_servers()
    _SESSION_CWD.reset(token)


def config(mode="workspace", root=None):
    project = {"mode": mode}
    if root:
        project["root"] = str(root)
    return {"command": sys.executable,
            "args": [str(Path(__file__).with_name("_project_mcp_server.py")), "${projectRoot}"],
            "project": project, "tools": {"include": ["source"]}, "connect_timeout": 10, "timeout": 10}


def call(name, root, args=None, **kwargs):
    token = set_session_cwd(str(root))
    try:
        return json.loads(registry.dispatch(name, args or {}, **kwargs))
    finally:
        _SESSION_CWD.reset(token)


def tool(names, suffix):
    return next(n for n in names if n.endswith(suffix))


def test_workspace_rpc_identity_is_immutable_and_isolated(projects):
    a, b, home = projects
    cfg = config()
    cfg["cwd"] = "${projectRoot}"
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"bound": cfg}}))
    names = discover_mcp_tools(["bound"])
    source = tool(names, "_source")
    assert not any(n.endswith("_retarget_project") for n in names)
    assert "Unknown tool" in registry.dispatch("mcp__bound__retarget_project", {})
    schema = copy.deepcopy(registry.get_schema(source))
    server = core._servers["bound"]
    result = call(source, a)
    assert json.loads(result["result"]) == {"source": "alpha", "root": str(a), "cwd": str(a)}
    assert result["project_identity"]["commit"] == git(a, "rev-parse", "HEAD")
    from tools.mcp_tool_discovery import get_mcp_status
    assert get_mcp_status()[0]["project_identity"] == result["project_identity"]
    with ThreadPoolExecutor(2) as pool:
        allowed = pool.submit(call, source, a)
        denied = pool.submit(call, source, b)
        assert allowed.result()["project_identity"]["matches_task"] is True
        assert denied.result()["project_binding_error"] is True
    # Discovery from another session neither swaps the connection nor edits prompt schemas.
    token = set_session_cwd(str(b))
    try:
        discover_mcp_tools(["bound"])
        other_names = register_mcp_servers({"second": cfg})
    finally:
        _SESSION_CWD.reset(token)
    assert core._servers["bound"] is server
    assert registry.get_schema(source) == schema
    second = next(n for n in other_names if "second" in n and n.endswith("_source"))
    assert json.loads(call(second, b)["result"])["source"] == "beta"
    # Explicit fixed-project mode remains useful from a different task, with provenance.
    fixed_names = register_mcp_servers({"fixed": config("fixed", a)})
    fixed = next(n for n in fixed_names if "fixed" in n and n.endswith("_source"))
    assert call(fixed, b)["project_identity"] == {
        "root": str(a), "commit": git(a, "rev-parse", "HEAD"), "mode": "fixed", "matches_task": False}
    from tools.terminal_tool import register_task_env_overrides, clear_task_env_overrides
    register_task_env_overrides("binding-child", {"cwd": str(b)})
    try:
        assert call(source, a, task_id="binding-child")["project_binding_error"] is True
    finally:
        clear_task_env_overrides("binding-child")
    # Resources and prompts traverse the same binding guard, before lazy/transport access.
    for suffix, args in [("_list_resources", {}), ("_read_resource", {"uri": "fixture://source"}),
                         ("_list_prompts", {}), ("_get_prompt", {"name": "source"})]:
        name = tool([n for n in names if "bound" in n], suffix)
        assert call(name, a, args)["project_identity"]["matches_task"] is True
        assert call(name, b, args)["project_binding_error"] is True
    # HEAD changes while an actual child request is in flight: never release stale results.
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, source, a, {"wait": True})
        deadline = time.monotonic() + 10
        while not (a / "entered").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (a / "entered").exists()
        (a / "symbols.txt").write_text("new-alpha")
        git(a, "commit", "-am", "new HEAD")
        (a / "release").touch()
        assert pending.result()["project_binding_error"] is True
    assert call(source, a)["project_binding_error"] is True
    assert registry.get_schema(source) == schema


def test_lazy_cache_and_invalid_launch_cannot_bypass_identity(projects):
    a, b, home = projects
    from tools.mcp_tool_project import prepare_project_config
    from tools.mcp_schema_cache import config_fingerprint, get_cached_entry
    cfg = config()
    names = register_mcp_servers({"lazybound": cfg})
    source = tool(names, "_source")
    old_handler = registry.get_entry(source).handler
    prepared = prepare_project_config(cfg)
    fingerprint = config_fingerprint(prepared)
    assert get_cached_entry("lazybound", fingerprint)
    token = set_session_cwd(str(b))
    try:
        assert config_fingerprint(prepare_project_config(cfg)) != fingerprint
    finally:
        _SESSION_CWD.reset(token)
    shutdown_mcp_servers()
    cfg["lazy"] = True
    register_mcp_servers({"lazybound": cfg})
    assert "lazybound" in core._lazy_server_configs
    assert "lazybound" not in core._servers
    assert call(source, b)["project_binding_error"] is True
    assert "lazybound" not in core._servers
    assert call(source, a)["project_identity"]["matches_task"] is True
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(home / "other-profile"))
    try:
        assert call(source, a)["project_binding_error"] is True
    finally:
        reset_hermes_home_override(token)
    # A retained handler must reject a connection rebuilt for another worktree.
    shutdown_mcp_servers()
    token = set_session_cwd(str(b))
    try:
        register_mcp_servers({"lazybound": cfg})
    finally:
        _SESSION_CWD.reset(token)
    assert json.loads(old_handler({}))["project_binding_error"] is True
    shutdown_mcp_servers()
    register_mcp_servers({"lazybound": cfg})  # A's cache was replaced by B, so eager A refreshes it.
    shutdown_mcp_servers()
    register_mcp_servers({"lazybound": cfg})
    assert "lazybound" in core._lazy_server_configs
    (a / "symbols.txt").write_text("changed")
    git(a, "commit", "-am", "changed")
    assert call(source, a)["project_binding_error"] is True
    assert "lazybound" not in core._servers
    invalid = config()
    invalid["args"][-1] = str(b)
    register_mcp_servers({"bad-target": invalid})
    assert "bad-target" not in core._servers
    assert "projectRoot" in core._server_connect_errors["bad-target"]
