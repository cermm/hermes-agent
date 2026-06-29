"""Exercise the actual LSP spawn boundary with synthetic credentials and real IPC."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent.lsp.client import LSPClient


MOCK_SERVER = str(Path(__file__).with_name("_mock_lsp_server.py"))
CREDENTIAL_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN", "TELEGRAM_BOT_TOKEN",
    "HERMES_DASHBOARD_SESSION_TOKEN", "GATEWAY_RELAY_AUDIT_TOKEN",
)


async def _child_environment(workspace, env, expected):
    document = workspace / "example.py"
    document.write_text("value = 1\n")
    client = LSPClient(
        server_id="environment-probe", workspace_root=str(workspace),
        command=[sys.executable, MOCK_SERVER], env=env,
    )
    await client.start()
    proc = client._proc
    try:
        assert client.is_running
        result = await asyncio.wait_for(client._send_request("test/environment", {
            "keys": list(CREDENTIAL_KEYS), "expected": expected, "cwd": str(workspace),
        }), timeout=5)
        version = await client.open_file(str(document), language_id="python")
        assert await client.wait_for_diagnostics(str(document), version)
        assert client.diagnostics_for(str(document)) == []
    finally:
        await client.shutdown()
    assert proc is not None and proc.returncode is not None
    assert client.state == "stopped"
    assert client._proc is None
    assert result["cwd_matches"]
    assert all(result["matches"].values())
    return result["present"]


@pytest.mark.asyncio
@pytest.mark.parametrize("env", [None, {}])
async def test_lsp_child_excludes_inherited_credentials(monkeypatch, tmp_path, env):
    for key in CREDENTIAL_KEYS:
        monkeypatch.setenv(key, "synthetic-parent-canary")
    monkeypatch.setenv("LSP_NONSECRET_SETTING", "inherited")
    expected = {"LSP_NONSECRET_SETTING": "inherited", "PATH": os.environ["PATH"]}

    present = await _child_environment(tmp_path, env, expected)

    assert not any(present.values()), present
    # Scrubbing the child must not mutate the gateway's environment.
    assert all(os.environ[key] == "synthetic-parent-canary" for key in CREDENTIAL_KEYS)


@pytest.mark.asyncio
async def test_lsp_child_preserves_explicit_server_credentials(monkeypatch, tmp_path):
    for key in CREDENTIAL_KEYS:
        monkeypatch.setenv(key, "synthetic-parent-canary")
    monkeypatch.setenv("LSP_NONSECRET_SETTING", "parent")
    explicit = {"OPENAI_API_KEY": "synthetic-explicit-server-value",
                "LSP_NONSECRET_SETTING": "server", "LSP_CUSTOM_OPT": "enabled"}

    present = await _child_environment(tmp_path, explicit, explicit)

    assert present["OPENAI_API_KEY"]
    assert not any(value for key, value in present.items() if key != "OPENAI_API_KEY"), present
