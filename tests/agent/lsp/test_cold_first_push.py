"""A late baseline seed cannot become a false clean verdict or lose recovery."""
import asyncio
from pathlib import Path
import sys

import pytest

from agent.lsp.client import LSPClient, LSPProtocolError


PEER = str(Path(__file__).with_name("_cold_seed_lsp_server.py"))
GOOD = 'count: int = 1\n'
BAD = 'count: int = "bad"\n'


async def late_edit(client, document, current=BAD):
    document.write_text(GOOD)
    await client.start()
    initial = await client.open_file(str(document), language_id="python")
    # The peer deliberately withholds all initial diagnostics; the baseline
    # expires independently of any wall-clock scheduling of the later seed.
    assert not await client.wait_for_diagnostics(str(document), initial, timeout=2)
    document.write_text(current)
    version = await client.open_file(str(document), language_id="python")
    return asyncio.create_task(client.wait_for_diagnostics(str(document), version))


async def state_after_recovery(client):
    deadline = asyncio.get_running_loop().time() + 4
    while True:
        state = await client._send_request("test/state", {})
        if len(state["changes"]) >= 2 or asyncio.get_running_loop().time() >= deadline:
            return state
        await asyncio.sleep(.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unversioned-stale", "unversioned-current", "unversioned-stale-error", "versioned-stale", "versioned-current", "versioned-future", "versioned-bool"])
async def test_late_seed_recovery_preserves_freshness_and_clean_repairs(tmp_path, kind):
    document = tmp_path / "source.py"
    client = LSPClient(server_id="cold-seed", workspace_root=str(tmp_path),
                       command=[sys.executable, PEER], seed_diagnostics_on_first_push=True)
    waiter = None
    try:
        current = GOOD if kind == "unversioned-stale-error" else BAD
        waiter = await late_edit(client, document, current)
        # Recovery must synchronize the last sent text, not read a newer,
        # unsynchronized disk edit behind the file-operation version boundary.
        document.write_text("unsynchronized disk content\n")
        await client._send_request("test/seed", {"kind": kind})
        if kind == "versioned-current":
            assert await waiter
            state = await client._send_request("test/state", {})
            assert len(state["changes"]) == 1
        else:
            state = await state_after_recovery(client)
            if len(state["changes"]) < 2:
                assert await waiter, "The late diagnostic never became fresh after the baseline expired"
            assert state["changes"] == [{"version": 1, "text": current}, {"version": 2, "text": current}]
            assert not waiter.done(), "Quarantined first payload satisfied the caller"
            assert not client._docs[str(document)].fresh()
            await client._send_request("test/release", {})
            assert await waiter
        assert state["pulls"] == 1
        fresh = client.diagnostics_for(str(document), fresh_only=True)
        assert (fresh[0]["code"] == "bad-assignment") if current == BAD else fresh == []
        # A real empty response after repair is checked clean; missing data above
        # remained unknown. The source bytes are only written by this test.
        await client._send_request("test/release", {})
        document.write_text(GOOD)
        version = await client.open_file(str(document))
        assert await client.wait_for_diagnostics(str(document), version)
        assert client._docs[str(document)].fresh()
        assert client.diagnostics_for(str(document), fresh_only=True) == []
        assert (await client._send_request("test/state", {}))["pulls"] == 1
    finally:
        if waiter and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        proc = client._proc
        await client.shutdown()
        assert proc is None or proc.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["cancel", "timeout", "clean-suppressed", "disconnect", "edit-race", "cancel-write", "deadline-write", "failed-write"])
async def test_seed_recovery_is_bounded_by_waiter_and_connection(tmp_path, finish):
    document = tmp_path / "source.py"
    client = LSPClient(server_id="cold-seed", workspace_root=str(tmp_path),
                       command=[sys.executable, PEER], seed_diagnostics_on_first_push=True)
    waiter = None
    others = []
    release = asyncio.Event()
    try:
        waiter = await late_edit(client, document, GOOD if finish == "clean-suppressed" else BAD)
        started = asyncio.get_running_loop().time()
        initial_version = client._docs[str(document)].version
        block_write = finish in {"edit-race", "cancel-write", "deadline-write", "failed-write"}
        entered = asyncio.Event()
        write = client._write

        async def held_write(message):
            if (message.get("method") == "textDocument/didChange"
                    and message["params"]["textDocument"]["version"] == initial_version + 1):
                entered.set()
                await release.wait()
                if finish == "failed-write":
                    raise BrokenPipeError("controlled write failure")
            await write(message)

        if block_write:
            client._write = held_write
        if finish == "deadline-write":
            # Recovery has only the remainder of the original five seconds.
            await asyncio.sleep(2)
        await client._send_request("test/seed", {"kind": "unversioned-current" if finish == "clean-suppressed" else "unversioned-stale"})
        if block_write:
            await asyncio.wait_for(entered.wait(), 4)
            assert not client._docs[str(document)].fresh()
            # Traffic observed before recovery transmission must not be relabeled.
            await client._send_request("test/publish", {"text": GOOD})
            assert not client._docs[str(document)].fresh()
        else:
            state = await state_after_recovery(client)
            assert len(state["changes"]) == 2 and state["pulls"] == 1
        if finish == "edit-race":
            other = asyncio.create_task(client.wait_for_diagnostics(str(document), initial_version))
            document.write_text(GOOD)
            edit = asyncio.create_task(client.open_file(str(document)))
            others += [other, edit]
            await asyncio.sleep(0)
            assert not edit.done()
            release.set()
            latest_version = await edit
            state = await client._send_request("test/state", {})
            versions = [row["version"] for row in state["changes"]]
            assert versions == sorted(set(versions)) and versions[-1] == latest_version
            assert [row["text"] for row in state["changes"]] == [BAD, BAD, GOOD]
            assert client._docs[str(document)].text == GOOD
            # A delayed explicit old generation cannot satisfy either waiter.
            await client._send_request("test/publish", {"text": BAD, "version": latest_version - 1})
            assert not client._docs[str(document)].fresh()
            assert not waiter.done() and not other.done()
            await client._send_request("test/release", {})
            assert await waiter and await other
            assert client.diagnostics_for(str(document), fresh_only=True) == []
            assert state["pulls"] == 1
        elif finish in {"cancel", "cancel-write"}:
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        elif finish in {"timeout", "deadline-write", "clean-suppressed"}:
            assert not await waiter  # no second publication: no checked-clean verdict
            if finish == "deadline-write":
                assert asyncio.get_running_loop().time() - started < 6.5
        elif finish == "failed-write":
            release.set()
            with pytest.raises(LSPProtocolError, match="document sync failed"):
                await waiter
        else:
            await client.shutdown()
            with pytest.raises(LSPProtocolError):
                await waiter
        if finish in {"cancel-write", "deadline-write", "failed-write"}:
            assert not client._docs[str(document)].fresh()
            # The cancelled/failed frame may or may not have reached the peer.
            # A later ordinary edit uses a higher version and full replacement.
            release.set()
            client._write = write
            document.write_text(GOOD)
            version = await client.open_file(str(document))
            await client._send_request("test/release", {})
            assert await client.wait_for_diagnostics(str(document), version)
            assert client.diagnostics_for(str(document), fresh_only=True) == []
        await client.shutdown()
        assert client._proc is None
        # The same client reconnects with no consumed-seed or pull-negotiation
        # state carried from its old connection.
        waiter = await late_edit(client, document)
        await client._send_request("test/seed", {"kind": "unversioned-current"})
        state = await state_after_recovery(client)
        assert len(state["changes"]) == 2 and state["pulls"] == 1
        await client._send_request("test/release", {})
        assert await waiter
        assert client.diagnostics_for(str(document), fresh_only=True)
    finally:
        release.set()
        tasks = others + ([waiter] if waiter else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        proc = client._proc
        await client.shutdown()
        assert proc is None or proc.returncode is not None
