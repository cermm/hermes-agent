"""Controlled real LSP peer: slow successful initialization, unavailable baseline."""
import argparse
import json
import os
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--log", required=True)
parser.add_argument("--delay", type=float, default=3.2)
parser.add_argument("--release-file")
parser.add_argument("--exit-on-open", action="store_true")
args = parser.parse_args()

def log(direction, message):
    with open(args.log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"direction": direction, "ns": time.monotonic_ns(),
                                 "pid": os.getpid(), "message": message}) + "\n")

def send(message):
    message = {"jsonrpc": "2.0", **message}
    log("out", message)
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()

while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise SystemExit(0)
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    log("in", message)
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        time.sleep(args.delay)
        send({"id": message["id"], "result": {"capabilities": {"textDocumentSync": 1}}})
    elif method == "textDocument/didOpen" and args.exit_on_open:
        break
    elif method == "textDocument/diagnostic":
        send({"id": message["id"], "error": {"code": -32601, "message": "push only"}})
    elif method == "textDocument/didChange" and (not args.release_file or os.path.exists(args.release_file)):
        document = params["textDocument"]
        text = params["contentChanges"][-1]["text"]
        diagnostics = [{"range": {"start": {"line": 0, "character": 0},
                                   "end": {"line": 0, "character": 5}},
                        "severity": 1, "code": 2322, "message": "bad assignment"}] if "bad" in text else []
        send({"method": "textDocument/publishDiagnostics", "params": {
            "uri": document["uri"], "version": document["version"], "diagnostics": diagnostics}})
    elif method == "shutdown":
        send({"id": message["id"], "result": None})
    elif method == "exit":
        break
