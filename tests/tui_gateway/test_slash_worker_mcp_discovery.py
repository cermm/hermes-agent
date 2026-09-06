"""Integration coverage for profile-local MCP discovery in slash workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import textwrap
import threading
from typing import TextIO

import pytest
import yaml

_mcp_server_mod = pytest.importorskip("mcp.server")

if not hasattr(_mcp_server_mod, "MCPServer"):
    # `mcp.server.MCPServer` replaced `mcp.server.fastmcp.FastMCP` in mcp 2.0.
    # Skip rather than fail on a FastMCP-era SDK: the probe below is written
    # against the 2.x API, and the pinned version provides it.
    pytest.skip(
        "profile-local MCP discovery probe requires mcp >= 2.0 (MCPServer)",
        allow_module_level=True,
    )


def _drain_queue(items: queue.Queue[str]) -> str:
    chunks: list[str] = []
    while True:
        try:
            chunks.append(items.get_nowait())
        except queue.Empty:
            return "".join(chunks)


def _collect_lines(stream: TextIO, items: queue.Queue[str]) -> None:
    for line in stream:
        items.put(line)


def test_profile_local_mcp_tool_is_visible_in_slash_worker(tmp_path):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    marker = "profile-local-61922"
    discovery_timeout = 15
    response_timeout = discovery_timeout + 30
    server = tmp_path / "mcp_probe.py"
    server.write_text(
        textwrap.dedent(
            f"""
            from mcp.server import MCPServer

            mcp = MCPServer("profileprobe")

            @mcp.tool()
            def hermes_61922_profile_probe() -> str:
                return {marker!r}

            if __name__ == "__main__":
                mcp.run(transport="stdio")
            """
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                # Profile discovery is the contract here, not the interactive
                # default's 1.5-second cold-start budget on a loaded runner.
                "mcp_discovery_timeout": discovery_timeout,
                "mcp_servers": {
                    "profileprobe": {
                        "enabled": True,
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
            env.pop(key)
    # SDK/provider fallbacks that use the process home must stay in the fixture.
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env["HERMES_HOME"] = str(profile_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    # This test covers profile-local MCP discovery, not orphan reaping. Keep the
    # watchdog from racing startup on loaded CI workers; watchdog behavior has
    # dedicated unit coverage in tests/test_slash_worker_watchdog.py.
    watchdog_timeout = str(response_timeout + 10)
    env["HERMES_SLASH_WATCHDOG_GRACE_S"] = watchdog_timeout
    env["HERMES_SLASH_WATCHDOG_POLL_S"] = watchdog_timeout
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "tui_gateway.slash_worker",
            "--session-key",
            "agent:main:tui:dm:mcp-profile-test",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    output: queue.Queue[str] = queue.Queue()
    stderr: queue.Queue[str] = queue.Queue()
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout = proc.stdout
        err = proc.stderr
        threading.Thread(
            target=lambda: output.put(stdout.readline()),
            daemon=True,
        ).start()
        threading.Thread(
            target=_collect_lines,
            args=(err, stderr),
            daemon=True,
        ).start()
        proc.stdin.write(json.dumps({"id": 1, "command": "/tools"}) + "\n")
        proc.stdin.flush()
        try:
            line = output.get(timeout=response_timeout)
        except queue.Empty:
            pytest.fail(
                f"slash worker produced no /tools response within {response_timeout} seconds; "
                f"returncode={proc.poll()!r}; stderr={_drain_queue(stderr)!r}"
            )
        response = json.loads(line)
        assert response["ok"] is True, (
            f"response={response!r}; stderr={_drain_queue(stderr)!r}"
        )
        assert "mcp__profileprobe__hermes_61922_profile_probe" in response["output"], (
            f"profile MCP tool missing; returncode={proc.poll()!r}; "
            f"stderr={_drain_queue(stderr)!r}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
