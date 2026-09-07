"""Pull negotiation must never starve push diagnostics or outlive a wait."""
import asyncio
import sys
from pathlib import Path

import pytest

from agent.lsp.client import LSPClient
from agent.lsp.protocol import LSPProtocolError


def peer(tmp_path, capability, mode="push"):
    return LSPClient(server_id="diagnostic-peer", workspace_root=str(tmp_path),
        command=[sys.executable, str(Path(__file__).with_name("_diagnostic_lsp_server.py"))],
        env={"DIAGNOSTIC_CAPABILITY": capability, "DIAGNOSTIC_MODE": mode})


async def change(client, path, text):
    path.write_text(text)
    return await client.open_file(str(path), language_id="python")


async def count(client):
    return await client._send_request("test/count", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,mode", [("missing", "push"), ("advertised", "push"),
                                            ("false", "push"), ("missing", "error"), ("missing", "invalid")])
async def test_push_only_is_bounded_and_waiters_end_with_the_connection(tmp_path, capability, mode):
    client = peer(tmp_path, capability, mode)
    path = tmp_path / "probe.py"
    await client.start()
    proc = client._proc
    try:
        for text in ("bad", "repaired", "bad again", "repaired again"):
            version = await change(client, path, text)
            assert await client.wait_for_diagnostics(str(path), version, timeout=3)
            assert bool(client.diagnostics_for(str(path), fresh_only=True)) == ("bad" in text)
        expected = (0 if capability == "false" else 1) if mode == "push" else 4
        assert await count(client) == expected

        version = await change(client, path, "silent")
        assert not await client.wait_for_diagnostics(str(path), version, timeout=.2)
        assert client.diagnostics_for(str(path), fresh_only=True) == []
        baseline_tasks = set(asyncio.all_tasks())
        waiter = asyncio.create_task(client.wait_for_diagnostics(str(path), version, timeout=10))
        await count(client)  # round trip lets the waiter start; no timing race
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not [t for t in asyncio.all_tasks() - baseline_tasks if not t.done()]
        assert await count(client) <= expected + (0 if mode == "push" else 2)

        waiter = asyncio.create_task(client.wait_for_diagnostics(str(path), version, timeout=10))
        await client._send_notification("test/disconnect", {})
        with pytest.raises(LSPProtocolError):
            await asyncio.wait_for(waiter, timeout=3)
        await asyncio.wait_for(proc.wait(), timeout=3)
    finally:
        await client.shutdown()

    # Same client object, new peer: no cached rejection or stale document tags.
    client._env["DIAGNOSTIC_MODE"] = "pull"
    client._env["DIAGNOSTIC_CAPABILITY"] = "advertised"
    await client.start()
    proc = client._proc
    try:
        version = await change(client, path, "bad after reconnect")
        assert await client.wait_for_diagnostics(str(path), version, timeout=3)
        assert client.diagnostics_for(str(path), fresh_only=True)
        assert await count(client) == 2  # ContentModified followed by a useful pull
    finally:
        await client.shutdown()
    assert proc.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["missing", "advertised"])
async def test_useful_pulls_retry_content_modified_and_clear_repairs(tmp_path, capability):
    client = peer(tmp_path, capability, "pull")
    path = tmp_path / "probe.py"
    await client.start()
    proc = client._proc
    try:
        for text in ("bad", "repaired"):
            version = await change(client, path, text)
            assert await client.wait_for_diagnostics(str(path), version, timeout=3)
            assert bool(client.diagnostics_for(str(path), fresh_only=True)) == (text == "bad")
        assert await count(client) == 3
        await client._send_request("test/set_capability", {"enabled": False})
        version = await change(client, path, "bad again")
        assert not await client.wait_for_diagnostics(str(path), version, timeout=.2)
        assert await count(client) == 3
        await client._send_request("test/set_capability", {"enabled": True})
        assert await client.wait_for_diagnostics(str(path), version, timeout=3)
        assert client.diagnostics_for(str(path), fresh_only=True)
        assert await count(client) == 4
        assert not client._pending
    finally:
        await client.shutdown()
    assert proc.returncode is not None
