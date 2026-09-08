"""A cancelled spawn owner must finish its waiters and reap its real child."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil

from agent.lsp import manager
from agent.lsp.manager import LSPService
from agent.lsp.servers import SERVERS, ServerDef, SpawnSpec


def test_cancelled_initialization_owns_child_and_settles_other_waiters(tmp_path, monkeypatch):
    assert Path(manager.__file__).resolve().is_relative_to(Path.cwd().resolve())
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    monkeypatch.chdir(project)
    source = project / "source.py"
    source.write_text("x = 1\n")
    wire = tmp_path / "wire.jsonl"
    peer = Path(__file__).with_name("_check_lsp_server.py")
    server = ServerDef("pyright", (".py",), lambda fp, root: root,
        lambda root, ctx: SpawnSpec([sys.executable, str(peer)], root, root,
            env={"CHECK_LOG": str(wire), "CHECK_STALL": "initialize"}))
    monkeypatch.setattr("agent.lsp.servers.SERVERS", [server, *[s for s in SERVERS if s.server_id != "pyright"]])
    service = LSPService(enabled=True, wait_mode="document", wait_timeout=5, install_strategy="manual", idle_timeout=0)
    child = None
    async def cancellation():
        owner = asyncio.create_task(service._get_or_spawn(str(source)))
        waiter = None
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                rows = [json.loads(line) for line in wire.read_text().splitlines() if line] if wire.exists() else []
                if any(row["message"].get("method") == "initialize" for row in rows):
                    break
                await asyncio.sleep(.02)
            else:
                raise AssertionError("real peer never received initialize")
            nonlocal child
            child = psutil.Process(rows[0]["pid"])
            waiter = asyncio.create_task(service._get_or_spawn(str(source)))
            await asyncio.sleep(0)
            futures = list(service._spawning.values())
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            await asyncio.wait({waiter}, timeout=2)
            return {"owner_cancelled": owner.cancelled(), "waiter_done": waiter.done(),
                    "spawn_futures_terminal": all(f.done() for f in futures), "pid": child.pid,
                    "created": child.create_time() if child.is_running() else None}
        finally:
            owner.cancel()
            if waiter is not None:
                waiter.cancel()
            await asyncio.gather(owner, *([waiter] if waiter is not None else []), return_exceptions=True)
    try:
        result = service._loop.run(cancellation(), timeout=15)
        service.shutdown()
        result["peer_reaped"] = not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        print("Cancelled initialization receipt:", json.dumps(result))
        assert result["owner_cancelled"] and result["waiter_done"] and result["spawn_futures_terminal"] and result["peer_reaped"], result
    finally:
        service.shutdown()
        if child is not None and child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            child.kill()


def test_cancelled_supplied_text_sync_never_attests_and_reaps_child(tmp_path, monkeypatch):
    from agent.lsp.client import LSPClient

    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    monkeypatch.chdir(project)
    source = project / "source.py"
    text = "x = 1\n"
    source.write_text(text)
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    wire = tmp_path / "wire.jsonl"
    peer = Path(__file__).with_name("_check_lsp_server.py")
    server = ServerDef("pyright", (".py",), lambda fp, root: root,
        lambda root, ctx: SpawnSpec([sys.executable, str(peer)], root, root,
            env={"CHECK_LOG": str(wire)}))
    monkeypatch.setattr("agent.lsp.servers.SERVERS", [server, *[s for s in SERVERS if s.server_id != "pyright"]])
    original_write = LSPClient._write
    reached = asyncio.Event()
    never = asyncio.Event()

    async def hold_completion(client, message):
        await original_write(client, message)
        if message.get("method") == "textDocument/didChange":
            reached.set()
            await never.wait()

    monkeypatch.setattr(LSPClient, "_write", hold_completion)
    service = LSPService(enabled=True, wait_mode="document", wait_timeout=5,
                         install_strategy="manual", idle_timeout=0)
    child = None

    async def cancellation():
        task = asyncio.create_task(service._query_outcome_async(str(source), read_only_text=text))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            rows = [json.loads(line) for line in wire.read_text().splitlines()]
            nonlocal child
            child = psutil.Process(rows[0]["pid"])
            client = next(iter(service._clients.values()))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled()
            assert client.diagnostic_snapshot(str(source)) is None
            assert client._docs[str(source)].sync_uncertain
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        service._loop.run(cancellation(), timeout=15)
        service.shutdown()
        rows = [json.loads(line) for line in wire.read_text().splitlines()]
        methods = [row["message"].get("method") for row in rows]
        assert "textDocument/didOpen" in methods and "textDocument/didChange" in methods
        assert "textDocument/didSave" not in methods
        assert (source.read_bytes(), source.stat().st_mtime_ns) == before
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        service.shutdown()
        if child is not None and child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            child.kill()
