"""Real stdio peer for explicit-check source, synchronization and lifecycle tests."""
import json
import os
from pathlib import Path
import sys
import time
from _mock_lsp_server import read_message, write_message

log = Path(os.environ["CHECK_LOG"])
def record(direction, message):
    with log.open("a") as stream:
        stream.write(json.dumps({"direction": direction, "message": message, "pid": os.getpid(), "at": time.monotonic()}) + "\n")
def send(message):
    record("out", message)
    write_message(message)

while True:
    message = read_message()
    if message is None:
        break
    record("in", message)
    method = message.get("method")
    if method == "initialize":
        if os.environ.get("CHECK_STALL") == "initialize":
            while True:
                time.sleep(.05)
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {"textDocumentSync": 1}}})
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        params = message["params"]
        td = params["textDocument"]
        text = td.get("text", params.get("contentChanges", [{}])[0].get("text", ""))
        if os.environ.get("CHECK_STALL") == "diagnostics":
            continue
        dependency = Path(os.environ.get("CHECK_DEPENDENCY", "missing-check-dependency"))
        bad = "bad" in text or (dependency.is_file() and "bad" in dependency.read_text())
        diagnostics = [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                        "message": "explicit check error", "severity": 1, "code": "CHECK001", "source": "check-peer"}] if bad else []
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": td["uri"], "diagnostics": diagnostics}})
    elif method == "textDocument/diagnostic":
        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "push only"}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        break
