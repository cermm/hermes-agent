"""Actual agent construction must deliver semantic workflow outside CLI posture."""
import json
from unittest.mock import patch

import pytest


@pytest.mark.parametrize("route", ["cli", "discord", "delegate"])
def test_file_capable_agent_receives_outcome_policy(tmp_path, monkeypatch, record_property, route):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "example.py").write_text("value: int = 1\n")
    if route == "delegate":
        from tests.tools.test_delegate_project_binding import build_child
        _, agent = build_child(monkeypatch, ["file"], isolated=True)
    else:
        from run_agent import AIAgent
        with patch("agent.process_bootstrap.OpenAI"), patch(
            "hermes_cli.config.load_config",
            return_value={"tools": {"tool_search": {"enabled": "off"}}, "model": {"context_length": 128000}},
        ):
            agent = AIAgent(model="fixture-model", provider="openai", api_key="fixture-key",
                            base_url="https://example.invalid/v1", enabled_toolsets=["file"],
                            platform=route, quiet_mode=True, skip_memory=True,
                            skip_context_files=True, max_iterations=1)
    try:
        prompt = agent._build_system_prompt()
        record_property("receipt", json.dumps({"route": route, "prompt": prompt,
            "tools": agent.tools, "skip_context_files": agent.skip_context_files}))
        assert "write_file" in agent.valid_tool_names and "patch" in agent.valid_tool_names
        assert "lsp_verification" in prompt, "File-capable workers need the structured outcome workflow in shared instructions"
        assert "baseline" in prompt.lower() and "delta" in prompt.lower()
    finally:
        agent.close()


@pytest.mark.parametrize("mode", ["direct", "deferred", "denied", "no_navigation", "no_tools"])
def test_session_schema_gates_navigation_without_global_catalog(tmp_path, monkeypatch, mode):
    from run_agent import AIAgent
    from tools.registry import registry
    from toolsets import TOOLSETS
    from agent.system_prompt import _semantic_guidance_block
    import model_tools

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    config = {"tools": {"tool_search": {"enabled": "on" if mode == "deferred" else "off"}},
              "model": {"context_length": 128000}}
    (home / "config.yaml").write_text(json.dumps(config))
    name = "mcp__guidance_fixture__find_symbol"
    group = "mcp-guidance_fixture"
    schema = {"name": name, "description": "Find a source symbol.",
              "parameters": {"type": "object", "properties": {}}}
    registry.register(name=name, toolset=group, schema=schema,
                      handler=lambda args, **kw: json.dumps({"symbol": "fixture"}))
    monkeypatch.setitem(TOOLSETS, group, {"tools": [name]})
    enabled = [] if mode == "no_tools" else ["file"]
    if mode in {"direct", "deferred", "denied"}:
        enabled.append(group)
    # A poisoned legacy global must not leak into this agent's policy.
    monkeypatch.setattr(model_tools, "_last_resolved_tool_names", {"mcp__other_profile__find_symbol"})
    with patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(model="gpt-4o", provider="openai", api_key="fixture-key",
                        base_url="https://example.invalid/v1", enabled_toolsets=enabled,
                        disabled_toolsets=[group] if mode == "denied" else [],
                        quiet_mode=True, skip_memory=True, skip_context_files=True)
    try:
        block = _semantic_guidance_block(agent) or ""
        prompt = agent._build_system_prompt()
        assert block in prompt
        assert "mcp__other_profile__find_symbol" not in block
        assert (name in block) is (mode == "direct")
        assert ("`tool_search`" in block) is (mode == "deferred")
        if mode == "deferred":
            assert name not in agent.valid_tool_names
            result, _ = model_tools._dispatch_bridge_tool("tool_search", {"queries": ["source symbol"]},
                                                        agent.enabled_toolsets, agent.disabled_toolsets)
            assert name in result
        assert "`terminal`" not in block
        if mode == "no_tools":
            assert block == ""
        else:
            assert "do not bypass tool restrictions" in block
    finally:
        agent.close()
        registry.deregister(name)
        model_tools._clear_tool_defs_cache()


@pytest.mark.parametrize("provider,model,v4a", [("openai", "gpt-4o", True), ("anthropic", "claude-sonnet-4", False)])
def test_resolved_patch_description_retains_truthful_outcomes(provider, model, v4a):
    from model_tools import get_tool_definitions, _clear_tool_defs_cache
    _clear_tool_defs_cache()
    with patch("agent.auxiliary_client._read_main_provider", return_value=provider), \
         patch("agent.auxiliary_client._read_main_model", return_value=model):
        schemas = get_tool_definitions(enabled_toolsets=["file"], quiet_mode=False)
    schema = next(d["function"] for d in schemas if d["function"]["name"] == "patch")
    assert ("mode" in schema["parameters"]["properties"]) is v4a
    assert "lsp_verification" in schema["description"]
    assert "empty delta is not a clean file" in schema["description"]
    assert "hermes lsp check" not in schema["description"], "No unconditional terminal cross-reference"


def test_cached_prompt_retains_policy_until_existing_compression_boundary(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from agent.conversation_loop import _restore_or_build_system_prompt
    from agent.system_prompt import invalidate_system_prompt
    from hermes_state import SessionDB
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("guidance-cache", source="cli", model="gpt-4o")
    with patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(model="gpt-4o", provider="openai", api_key="fixture-key",
                        base_url="https://example.invalid/v1", enabled_toolsets=["file"],
                        quiet_mode=True, skip_memory=True, skip_context_files=True,
                        session_id="guidance-cache", session_db=db)
    try:
        _restore_or_build_system_prompt(agent, None, [])
        original = agent._cached_system_prompt
        assert "lsp_verification" in original
        # No prompt rebuild is added to the tool refresh lifecycle.
        agent.valid_tool_names = {"read_file", "terminal"}
        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "Continue"}])
        assert agent._cached_system_prompt == original
        invalidate_system_prompt(agent)
        rebuilt = agent._build_system_prompt()
        assert "hermes lsp check" in rebuilt
        assert "lsp_verification" in rebuilt
    finally:
        agent.close()
        db.close()


@pytest.mark.parametrize("policy", ["available", "disabled", "no_skill_tools"])
def test_skill_pointer_uses_actual_profile_filtered_index(tmp_path, monkeypatch, policy):
    from tests.skills.test_semantic_code_intelligence_skill import _configure, _seed, NAME
    from run_agent import AIAgent
    from agent.prompt_builder import clear_skills_system_prompt_cache
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    profile = _configure(tmp_path, monkeypatch, {"disabled": [NAME]} if policy == "disabled" else {})
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _seed(profile)
    clear_skills_system_prompt_cache(clear_snapshot=True)
    with patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(model="gpt-4o", provider="openai", api_key="fixture-key",
                        base_url="https://example.invalid/v1",
                        enabled_toolsets=["file"] if policy == "no_skill_tools" else ["file", "skills"],
                        quiet_mode=True, skip_memory=True, skip_context_files=True)
    try:
        prompt = agent._build_system_prompt()
        pointer = 'skill_view(name="semantic-code-intelligence")'
        assert (pointer in prompt) is (policy == "available")
        assert "lsp_verification" in prompt, "Skill exclusion must not disable the basic workflow"
    finally:
        agent.close()
