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

import pytest
import yaml

pytest.importorskip("mcp.server.fastmcp")


def _drain_queue(items: queue.Queue[str]) -> str:
    chunks: list[str] = []
    while True:
        try:
            chunks.append(items.get_nowait())
        except queue.Empty:
            return "".join(chunks)


def test_profile_local_mcp_tool_is_visible_in_slash_worker(tmp_path):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    marker = "profile-local-61922"
    server = tmp_path / "fastmcp_probe.py"
    server.write_text(
        textwrap.dedent(
            f"""
            from mcp.server.fastmcp import FastMCP

            mcp = FastMCP("profileprobe")

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
    env["HERMES_HOME"] = str(profile_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    # This test covers profile-local MCP discovery, not orphan reaping. Keep the
    # watchdog from racing startup on loaded CI workers; watchdog behavior has
    # dedicated unit coverage in tests/test_slash_worker_watchdog.py.
    env["HERMES_SLASH_WATCHDOG_GRACE_S"] = "30"
    env["HERMES_SLASH_WATCHDOG_POLL_S"] = "30"
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
            target=lambda: stderr.put(err.read()),
            daemon=True,
        ).start()
        proc.stdin.write(json.dumps({"id": 1, "command": "/tools"}) + "\n")
        proc.stdin.flush()
        try:
            line = output.get(timeout=30)
        except queue.Empty:
            pytest.fail(
                "slash worker produced no /tools response within 30 seconds; "
                f"returncode={proc.poll()!r}; stderr={_drain_queue(stderr)!r}"
            )
        response = json.loads(line)
        assert response["ok"] is True
        assert "mcp__profileprobe__hermes_61922_profile_probe" in response["output"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
