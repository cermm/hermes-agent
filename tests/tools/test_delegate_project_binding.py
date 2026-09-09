"""Actual MCP transport and child schema lifecycle for isolated delegates (#37)."""
import copy
import contextvars
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.tools.test_mcp_project_binding import projects, config, call, git
from tools import mcp_tool as core
from tools.mcp_tool_discovery import register_mcp_servers
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from tools.registry import registry


def build_child(monkeypatch, enabled, *, isolated=True, disabled=None, requested=None, deferred=False):
    from tools import delegate_tool as dt
    import tools.delegate_tool_config as cfg
    settings = {"worktree_isolation": isolated, "inherit_mcp_toolsets": True}
    monkeypatch.setattr(cfg, "_load_config", lambda: settings)
    monkeypatch.setattr(dt, "_load_config", lambda: settings)
    parent = SimpleNamespace(enabled_toolsets=enabled, disabled_toolsets=disabled or [],
        valid_tool_names={e.name for e in registry.get_all_entries()}, model="fixture-model",
        provider="openai", api_key="fixture-key", base_url="https://example.invalid/v1",
        api_mode="chat_completions", platform="cli", session_id="fixture-parent")
    # Only the unused model transport/config are substituted. AIAgent and tool
    # definitions/registry selection are real; no network/model turn is made.
    with patch("agent.process_bootstrap.OpenAI"), patch("hermes_cli.config.load_config", return_value={"tools": {"tool_search": {"enabled": "on" if deferred else "off"}}, "model": {"context_length": 128000}}):
        child = dt._build_child_agent(0, "Inspect source", None, requested, None, 2, 1, parent)
    return parent, child


def names(agent):
    return {d["function"]["name"] for d in agent.tools}


def refreshes(agent):
    from agent.turn_context import _refresh_mcp_tools_between_turns
    from agent.conversation_compression import _refresh_agent_tool_definitions
    _refresh_mcp_tools_between_turns(agent)
    yield names(agent)
    _refresh_agent_tool_definitions(agent)
    yield names(agent)


def test_pending_workspace_discovery_never_enters_isolated_child(projects, monkeypatch, tmp_path):
    a, b, home = projects
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (home / "config.yaml").write_text(json.dumps({"tools": {"tool_search": {"enabled": "off"}}}))
    gate = tmp_path / "init-release"
    cfg = config()
    cfg["env"] = {"PROJECT_FIXTURE_INIT_GATE": str(gate)}
    parent_env = {key: os.environ.get(key) for key in ("HERMES_HOME", "HERMES_PROFILE", "TERMINAL_CWD")}
    child = None
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(contextvars.copy_context().run, register_mcp_servers, {"latebound": cfg})
        try:
            deadline = time.monotonic() + 10
            while not gate.with_suffix(".entered").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert gate.with_suffix(".entered").exists(), "actual initialize request must reach peer"
            assert "latebound" in core._server_connecting
            assert "latebound" not in core._servers
            parent, child = build_child(monkeypatch, ["file", "mcp-latebound"])
            initial = names(child)
            assert not any(n.startswith("mcp__latebound__") for n in initial)
            parent_selection = copy.deepcopy(parent.enabled_toolsets)
            gate.touch()
            discovered = pending.result(timeout=15)
            source = next(n for n in discovered if n.endswith("latebound__source"))
            assert json.loads(call(source, a)["result"])["source"] == "alpha"
            assert call(source, b)["project_binding_error"] is True
            from model_tools import get_tool_definitions
            parent_names = {d["function"]["name"] for d in get_tool_definitions(
                enabled_toolsets=parent.enabled_toolsets, quiet_mode=True)}
            assert source in parent_names, "parent gains actual late server"
            for current in refreshes(child):
                assert source not in current, "late workspace tools must stay absent before API schema/after compaction"
                assert "read_file" in current
            assert "mcp-latebound" in child.disabled_toolsets
            assert parent.enabled_toolsets == parent_selection
            assert parent.disabled_toolsets == []
            assert {key: os.environ.get(key) for key in parent_env} == parent_env
        finally:
            gate.touch()
            if child is not None:
                child.close()


@pytest.mark.parametrize("selection", ["canonical", "alias", "composite", "all", "narrow"])
@pytest.mark.parametrize("isolated", [True, False])
def test_effective_schema_preserves_grants_and_fixed_unbound(projects, monkeypatch, selection, isolated):
    a, b, home = projects
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (home / "config.yaml").write_text(json.dumps({"tools": {"tool_search": {"enabled": "off"}}}))
    generic = config()
    generic.pop("project")
    generic["args"][-1] = str(a)
    registered = register_mcp_servers({"matrixbound": config(), "matrixfixed": config("fixed", a),
                                       "matrixgeneric": generic})
    bound = next(n for n in registered if n.endswith("matrixbound__source"))
    fixed = next(n for n in registered if n.endswith("matrixfixed__source"))
    unbound = next(n for n in registered if n.endswith("matrixgeneric__source"))
    from toolsets import TOOLSETS
    monkeypatch.setitem(TOOLSETS, "fixture-bundle", {"tools": [bound, fixed, unbound, "read_file", "web_search"]})
    enabled = ["file", "web", "mcp-matrixbound", "mcp-matrixfixed", "mcp-matrixgeneric"]
    if selection == "alias":
        enabled = ["file", "web", "matrixbound", "matrixfixed", "matrixgeneric"]
    elif selection == "composite":
        enabled = ["fixture-bundle"]
    elif selection == "all":
        enabled = None
    parent, child = build_child(monkeypatch, enabled, isolated=isolated, disabled=["web"],
                                requested=["file"] if selection == "narrow" else None)
    try:
        for current in [names(child), *refreshes(child)]:
            assert (bound in current) is (not isolated)
            assert fixed in current and unbound in current
            assert "read_file" in current and "web_search" not in current
        assert parent.disabled_toolsets == ["web"]
        assert json.loads(call(fixed, b)["result"])["source"] == "alpha"
        assert call(fixed, b)["project_identity"]["matches_task"] is False
        if isolated:
            assert "Workspace-bound MCP navigation is unavailable" in child.ephemeral_system_prompt
    finally:
        child.close()


def test_default_deferred_catalog_cannot_restore_workspace_tools(projects, monkeypatch):
    a, b, home = projects
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (home / "config.yaml").write_text("{}")
    registered = register_mcp_servers({"deferredbound": config(), "deferredfixed": config("fixed", a)})
    bound = next(n for n in registered if n.endswith("deferredbound__source"))
    fixed = next(n for n in registered if n.endswith("deferredfixed__source"))
    parent, child = build_child(monkeypatch, ["file", "mcp-deferredbound", "mcp-deferredfixed"], deferred=True)
    from model_tools import _dispatch_bridge_tool
    from agent.tool_executor import _unwrap_tool_search_call
    try:
        for current in [names(child), *refreshes(child)]:
            assert "tool_search" in current
            assert bound not in current
        assert bound not in json.dumps(child.tools)
        assert fixed in json.dumps(child.tools)
        scoped = (child.enabled_toolsets, child.disabled_toolsets)
        search, _ = _dispatch_bridge_tool("tool_search", {"queries": ["source"]}, *scoped)
        assert bound not in search and fixed in search
        for tool in (bound, fixed):
            args = {"name": tool, "arguments": {}}
            response, routed = _dispatch_bridge_tool("tool_call", args, *scoped)
            _, _, blocked = _unwrap_tool_search_call(child, "tool_call", args)
            if tool == bound:
                assert routed is None and "not available" in response and blocked
            else:
                assert routed == (fixed, {}) and response is None and blocked is None
    finally:
        child.close()


def test_declaration_cleanup_name_reuse_and_profile_scope(projects, monkeypatch):
    a, b, home = projects
    from tools.mcp_tool_project import workspace_bound_toolsets
    register_mcp_servers({"reuse": config()})
    assert "mcp-reuse" in workspace_bound_toolsets()
    other = home.parent / "another-profile"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert "mcp-reuse" not in workspace_bound_toolsets()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutdown_mcp_servers()
    assert "reuse" not in core._server_project_modes
    register_mcp_servers({"reuse": config("fixed", a)})
    assert "mcp-reuse" not in workspace_bound_toolsets()
    shutdown_mcp_servers()
    register_mcp_servers({"reuse": config()})
    assert "mcp-reuse" in workspace_bound_toolsets()



def test_lazy_publication_is_declared_before_first_connection(projects):
    from tools.mcp_tool_project import workspace_bound_toolsets
    cfg = config()
    register_mcp_servers({"lazydeclared": cfg})
    shutdown_mcp_servers()
    cfg["lazy"] = True
    register_mcp_servers({"lazydeclared": cfg})
    assert "lazydeclared" in core._lazy_server_configs and "lazydeclared" not in core._servers
    assert "mcp-lazydeclared" in workspace_bound_toolsets()
    a, b, home = projects
    source = next(n for n in core._lazy_server_tool_names["lazydeclared"] if n.endswith("__source"))
    assert call(source, b)["project_binding_error"] is True
    assert "lazydeclared" not in core._servers
    assert call(source, a)["project_identity"]["matches_task"] is True


def test_scoped_shutdown_preserves_other_profile_declaration(projects, monkeypatch):
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from tools.mcp_tool_project import workspace_bound_toolsets
    a, b, home = projects
    other = home.parent / "multiplex-other"
    other.mkdir()
    # Exercise real scoped registry/server teardown without activating a live
    # gateway multiplexer. Scope identity follows the real profile ContextVar.
    monkeypatch.setattr(core, "_mcp_registry_scope", lambda: str(get_hermes_home().resolve()))
    register_mcp_servers({"scopea": config()})
    token = set_hermes_home_override(str(other))
    try:
        register_mcp_servers({"scopeb": config()})
        assert "mcp-scopea" not in workspace_bound_toolsets()
        assert "mcp-scopeb" in workspace_bound_toolsets()
        shutdown_mcp_servers(scope=str(home))
        assert "scopea" not in core._server_project_modes
        assert "scopeb" in core._server_project_modes and "scopeb" in core._servers
        assert core._servers["scopeb"].session is not None
    finally:
        reset_hermes_home_override(token)
