"""Actual CLI startup; only final chat/model callback is replaced for #37 proof."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import psutil
from hermes_cli import main, mcp_startup
from tools import mcp_tool as core, mcp_tool_discovery as discovery
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from agent.runtime_cwd import resolve_agent_cwd, set_session_cwd, _SESSION_CWD
from hermes_cli.config import load_config
from tools.registry import registry

profile = Path(os.environ["HERMES_HOME"])
report = {"argv": sys.argv, "pid": os.getpid(), "created": psutil.Process().create_time(),
          "source": str(REPO), "imports": {m.__name__: m.__file__ for m in (main, discovery)},
          "startup": [], "rpc": []}
assert all(Path(v).resolve().is_relative_to(REPO) for v in report["imports"].values())
original = discovery.discover_mcp_tools

def observed(*args, **kwargs):
    report["startup"].append({"cwd": os.getcwd(), "effective_cwd": str(resolve_agent_cwd()),
        "home": os.environ["HERMES_HOME"], "filter": kwargs.get("allowed_mcp_names", args[0] if args else None)})
    return original(*args, **kwargs)
discovery.discover_mcp_tools = observed
assert core._ensure_mcp_sdk()
Session = core.ClientSession
class ObservedSession(Session):
    async def send_request(self, request, *args, **kwargs):
        item = {"request": request.model_dump(mode="json", by_alias=True)}
        report["rpc"].append(item)
        result = await super().send_request(request, *args, **kwargs)
        item["response"] = result.model_dump(mode="json", by_alias=True)
        return result
core.ClientSession = ObservedSession


def chat(args):
    mcp_startup.wait_for_mcp_discovery(timeout=20, single_query=True)
    cfg = load_config()
    from model_tools import get_tool_definitions
    selected = args.toolsets.split(",")
    denied = cfg.get("agent", {}).get("disabled_toolsets", [])
    schemas = get_tool_definitions(selected, denied, quiet_mode=True, skip_tool_search_assembly=True)
    names = {d["function"]["name"] for d in schemas}
    source = next(n for n in names if n.endswith("__source"))
    result = json.loads(registry.dispatch(source, {}))
    other = cfg["routing_fixture"]["other"]
    token = set_session_cwd(other)
    try:
        rejected = json.loads(registry.dispatch(source, {}))
    finally:
        _SESSION_CWD.reset(token)
    assert rejected.get("project_binding_error") is True
    report.update(outcome="PASS", parsed_model=args.model, parsed_provider=args.provider,
        parsed_reasoning=args.reasoning, model_config=cfg["model"], selected=selected, denied=denied,
        schemas=schemas, result=result, wrong_root=rejected,
        effective_cwd=str(resolve_agent_cwd()), cwd=os.getcwd(),
        env={k: os.environ.get(k) for k in ("HERMES_HOME", "HERMES_PROFILE", "TERMINAL_CWD",
             "HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")},
        binding=core._servers[cfg["routing_fixture"]["server"]]._config["_project_binding"])
    # Capture the actual shared prompt after production CLI/profile resolution.
    # Only the unused provider client is replaced; there is no model call here.
    from run_agent import AIAgent
    from unittest.mock import patch
    with patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(model=args.model, provider=args.provider, api_key="fixture-key",
                        base_url="https://example.invalid/v1", enabled_toolsets=selected,
                        disabled_toolsets=denied, platform="cli", quiet_mode=True,
                        skip_memory=True, skip_context_files=True)
    try:
        report["system_prompt"] = agent._build_system_prompt()
        report["agent_schemas"] = agent.tools
        assert "lsp_verification" in report["system_prompt"]
        assert ("hermes lsp check" in report["system_prompt"]) == ("terminal" in agent.valid_tool_names)
    finally:
        agent.close()
    return 0
main.cmd_chat = chat
try:
    main.main()
    assert report.get("outcome") == "PASS"
finally:
    owned = [(p.pid, p.create_time()) for p in psutil.Process().children(recursive=True)]
    shutdown_mcp_servers()
    remaining = []
    for pid, created in owned:
        try:
            process = psutil.Process(pid)
            if process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE:
                remaining.append(pid)
        except psutil.NoSuchProcess:
            pass
    report["cleanup"] = {"observed": owned, "remaining": remaining}
    (profile / "routing-receipt.json").write_text(json.dumps(report, indent=2))
    assert not remaining
