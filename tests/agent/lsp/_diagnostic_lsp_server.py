"""Real stdio peer with independently scheduled pushes and counted pull requests."""
import json
import os
import threading

from _mock_lsp_server import read_message, write_message


lock = threading.Lock()
documents = {}
pulls = 0
capability_request = None
mode = os.environ.get("DIAGNOSTIC_MODE", "push")


def send(message):
    with lock:
        write_message({"jsonrpc": "2.0", **message})


def diagnostics(text):
    return [{"range": {"start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1}},
             "severity": 1, "code": "BAD", "message": "bad assignment"}] if "bad" in text else []


while (msg := read_message()) is not None:
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        capabilities = {"textDocumentSync": 1}
        advertised = os.environ.get("DIAGNOSTIC_CAPABILITY", "missing")
        if advertised != "missing":
            capabilities["diagnosticProvider"] = False if advertised == "false" else {}
        send({"id": msg["id"], "result": {"capabilities": capabilities}})
    elif method in {"textDocument/didOpen", "textDocument/didChange"}:
        doc = params["textDocument"]
        text = doc.get("text", "") if method.endswith("didOpen") else params["contentChanges"][0]["text"]
        documents[doc["uri"]] = diagnostics(text)
        if mode in {"push", "error", "invalid"} and "silent" not in text:
            # The reader remains available to reject pulls during the delayed push.
            timer = threading.Timer(.2, send, args=({"method": "textDocument/publishDiagnostics",
                "params": {"uri": doc["uri"], "version": doc["version"],
                           "diagnostics": diagnostics(text)}},))
            timer.daemon = True
            timer.start()
    elif method == "textDocument/diagnostic":
        pulls += 1
        if mode == "push":
            send({"id": msg["id"], "error": {"code": -32601, "message": "unsupported"}})
        elif mode == "error":
            send({"id": msg["id"], "error": {"code": -32000, "message": "temporarily unavailable"}})
        elif mode == "invalid":
            send({"id": msg["id"], "result": None})
        elif pulls == 1:
            send({"id": msg["id"], "error": {"code": -32801, "message": "changed"}})
        else:
            send({"id": msg["id"], "result": {"kind": "full", "items": documents[params["textDocument"]["uri"]]}})
    elif method == "test/count":
        send({"id": msg["id"], "result": pulls})
    elif method == "test/set_capability":
        capability_request = msg["id"]
        enabled = params["enabled"]
        send({"id": "capability", "method": "client/registerCapability" if enabled else "client/unregisterCapability",
              "params": {"registrations" if enabled else "unregisterations": [
                  {"id": "pull", "method": "textDocument/diagnostic"}]}})
    elif msg.get("id") == "capability":
        send({"id": capability_request, "result": None})
    elif method == "test/disconnect":
        break
    elif method == "shutdown":
        send({"id": msg["id"], "result": None})
    elif method == "exit":
        break
