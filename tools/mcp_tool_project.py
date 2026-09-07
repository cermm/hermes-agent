"""Opt-in, immutable local Git project binding for the existing MCP lifecycle.

A binding is a launch contract, not attestation of arbitrary server internals. Servers
must honor their project argument and must not expose tools that retarget the process.
"""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

from tools.registry import tool_error


class ProjectBindingError(ValueError):
    pass


def _identity(path: str) -> dict:
    from hermes_cli._subprocess_compat import harden_git_argv, noninteractive_git_env
    env = {k: v for k, v in noninteractive_git_env().items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", *harden_git_argv(["rev-parse", "--show-toplevel", "HEAD"])],
            cwd=Path(path).expanduser().resolve(strict=True), env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        root, commit = result.stdout.strip().splitlines() if result.returncode == 0 else ("", "")
        if not root or len(commit) not in (40, 64):
            raise ValueError("not a committed Git worktree")
        return {"root": str(Path(root).resolve(strict=True)), "commit": commit}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ProjectBindingError(f"Cannot verify MCP project root and HEAD at {path}: {exc}") from exc


def _task_identity(task_id=None) -> dict:
    from agent.runtime_cwd import resolve_agent_cwd
    from tools.file_tools_paths import _authoritative_workspace_root, _terminal_env_type_for_task
    task_id = task_id or "default"
    if _terminal_env_type_for_task(task_id) != "local":
        raise ProjectBindingError("MCP project binding requires a local task worktree")
    # Raw task ids carry delegated/desktop overrides; otherwise honor the logical session cwd.
    root = _authoritative_workspace_root(task_id) if task_id != "default" else None
    return _identity(root or str(resolve_agent_cwd()))


def prepare_project_config(config: dict) -> dict:
    """Freeze identity before eager or lazy registration. Unbound configs are unchanged."""
    project = config.get("project")
    if project is None:
        return config
    if not isinstance(project, dict) or project.get("mode") not in {"workspace", "fixed"}:
        raise ProjectBindingError("MCP project.mode must be workspace or fixed")
    if not config.get("command") or config.get("url"):
        raise ProjectBindingError("MCP project binding supports local stdio servers only")
    mode = project["mode"]
    if mode == "workspace" and project.get("root"):
        raise ProjectBindingError("Workspace project binding derives root from the task; omit project.root")
    if mode == "fixed" and not project.get("root"):
        raise ProjectBindingError("Fixed project binding requires project.root")
    identity = _task_identity() if mode == "workspace" else _identity(project["root"])
    args = config.get("args") or []
    if not isinstance(args, list) or "${projectRoot}" not in args:
        raise ProjectBindingError("Bound MCP args must contain a whole ${projectRoot} argument for the server target")
    cfg = copy.deepcopy(config)
    cfg["args"] = [identity["root"] if arg == "${projectRoot}" else arg for arg in args]
    cwd = cfg.get("cwd", "${projectRoot}")
    if cwd != "${projectRoot}" and str(Path(cwd).resolve()) != identity["root"]:
        raise ProjectBindingError("Bound MCP cwd must equal ${projectRoot}")
    cfg["cwd"] = identity["root"]
    from hermes_constants import get_hermes_home
    cfg["_project_binding"] = {**identity, "mode": mode, "profile": str(get_hermes_home().resolve())}
    return cfg


def validate_launch(config: dict) -> dict:
    """Preserve frozen lazy descriptors across the delay before actual spawn."""
    if config.get("project") is None:
        return config
    if "_project_binding" not in config:
        return prepare_project_config(config)
    binding = config["_project_binding"]
    if _identity(binding["root"]) != {k: binding[k] for k in ("root", "commit")}:
        raise ProjectBindingError("MCP project HEAD changed since discovery; start a new worker session")
    if config.get("cwd") != binding["root"] or binding["root"] not in config.get("args", []):
        raise ProjectBindingError("MCP project launch target no longer matches its frozen identity")
    return config


def _check_binding(name, binding, task_id) -> dict:
    from hermes_constants import get_hermes_home
    from tools.mcp_tool_common import _core
    if str(get_hermes_home().resolve()) != binding["profile"]:
        raise ProjectBindingError("MCP project connection belongs to another profile")
    with _core._lock:
        server = _core._servers.get(name)
        cfg = getattr(server, "_config", None) if server else _core._lazy_server_configs.get(name)
    if not isinstance(cfg, dict) or cfg.get("_project_binding") != binding:
        raise ProjectBindingError("MCP project connection identity differs from the registered tool snapshot")
    validate_launch(cfg)
    try:
        current = _task_identity(task_id)
    except ProjectBindingError:
        if binding["mode"] == "workspace":
            raise
        current = None
    matches = current == {k: binding[k] for k in ("root", "commit")}
    if binding["mode"] == "workspace" and not matches:
        raise ProjectBindingError(
            f"MCP project mismatch: connection targets {binding['root']} at {binding['commit']}; "
            f"task targets {current}. Use a separate worker process or separately named server. "
            "The existing connection and conversation tool snapshot were not retargeted.")
    return {k: binding[k] for k in ("root", "commit", "mode")} | {"matches_task": matches}


def bind_handler(name, config, handler):
    """Guard all registered RPC families before spawn and after the response, without schema changes."""
    binding = copy.deepcopy(config.get("_project_binding"))
    if binding is None:
        return handler

    def bound(args, **kwargs):
        try:
            _check_binding(name, binding, kwargs.get("task_id"))
            result = handler(args, **kwargs)
            identity = _check_binding(name, binding, kwargs.get("task_id"))
        except ProjectBindingError as exc:
            return tool_error(str(exc), project_binding_error=True)
        payload = json.loads(result)
        payload["project_identity"] = identity
        return json.dumps(payload, ensure_ascii=False)
    return bound
