"""TypeScript SDK prerequisites shared by registry spawning and read-only status.

A language-server wrapper is not a TypeScript SDK. Resolve the JavaScript
entrypoint required by typescript-language-server, without running npm or Node.
This is a prerequisite check, not proof that initialization/diagnostics succeeded.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

SDK_REPAIR = (
    "Install a compatible SDK with lib/tsserver.js (the managed recipe uses "
    "typescript-language-server@5.3.0 and typescript@6.0.3), or set "
    "lsp.servers.typescript.initialization_options.tsserver.path to that file "
    "or its lib directory. Binary presence does not prove diagnostic readiness."
)
_MODULE_FOLDERS = ("node_modules/typescript/lib", ".vscode/pnpify/typescript/lib", ".yarn/sdks/typescript/lib")


def _sdk_file(value: str, root: str) -> Path | None:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path(shutil.which(value) or os.path.join(root, value))
    candidate = candidate.resolve()
    if candidate.is_dir():
        candidate = candidate / "tsserver.js"
    if candidate.name != "tsserver.js" or not candidate.is_file():
        return None
    # TLS itself needs package.json to identify the SDK version.
    package = candidate.parent.parent / "package.json"
    if not package.is_file() and candidate.parent.parent.name == "built":
        package = candidate.parent.parent.parent / "package.json"
    try:
        version = json.loads(package.read_text(encoding="utf-8")).get("version", "")
        if not isinstance(version, str) or not version.split(".")[0].isdigit():
            return None
    except (OSError, ValueError, AttributeError):
        return None
    return candidate


def resolve_sdk(root: str, binary: str | None, initialization_options: dict[str, Any]) -> tuple[str, str]:
    """Resolve explicit → workspace → fallback → wrapper SDK → current profile SDK.

    Invalid explicit paths fail visibly instead of silently selecting another SDK.
    Workspace lookup includes Node/Yarn SDK directories supported by TLS. Only
    successful filesystem preflight is claimed; callers still initialize the server.
    """
    tsserver = initialization_options.get("tsserver") or {}
    explicit = tsserver.get("path")
    if explicit:
        sdk = _sdk_file(str(explicit), root)
        if sdk is None:
            raise ValueError(f"Configured TypeScript tsserver.path is unusable: {explicit}. {SDK_REPAIR}")
        return str(sdk), "configured"
    project = Path(root).resolve()
    for directory in (project, *project.parents):
        for module in _MODULE_FOLDERS:
            sdk = _sdk_file(str(directory / module), root)
            if sdk is not None:
                return str(sdk), "workspace"
    fallback = tsserver.get("fallbackPath")
    if fallback:
        sdk = _sdk_file(str(fallback), root)
        if sdk is None:
            raise ValueError(f"Configured TypeScript tsserver.fallbackPath is unusable: {fallback}. {SDK_REPAIR}")
        return str(sdk), "configured-fallback"
    if binary:
        executable = Path(shutil.which(binary) or binary).resolve()
        for directory in executable.parents:
            if directory.name == "node_modules":
                sdk = _sdk_file(str(directory / "typescript/lib"), root)
                if sdk is not None:
                    return str(sdk), "server-package"
    sdk = _sdk_file(str(get_hermes_home() / "lsp/node_modules/typescript/lib"), root)
    if sdk is not None:
        return str(sdk), "profile"
    raise ValueError(f"TypeScript wrapper found but no usable SDK with lib/tsserver.js. {SDK_REPAIR}")


def backend_status(root: str, command: list[str] | None = None,
                   initialization_options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inspect prerequisites for the current workspace/profile; never install or spawn."""
    from agent.lsp.install import _existing_binary
    binary = (shutil.which(command[0]) or command[0]) if command else _existing_binary("typescript-language-server")
    if not binary or not os.path.isfile(binary):
        return {"status": "binary-missing", "message": "TypeScript language-server command is missing."}
    try:
        sdk, source = resolve_sdk(root, binary, initialization_options or {})
    except ValueError as exc:
        return {"status": "sdk-unavailable", "binary": binary, "message": str(exc)}
    return {"status": "prerequisites-present", "binary": binary, "sdk_path": sdk, "sdk_source": source,
            "message": "SDK entrypoint found; initialization and fresh diagnostics have not been tested by this status check."}
