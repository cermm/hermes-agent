"""Real stdio protocol fixture: reports only the source at its supplied launch target."""
import json
import os
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    method = req["method"]
    params = req.get("params", {})
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {
            "tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": "project-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"ttlMs": 600000, "tools": [{"name": "source", "description": "Read project source", "inputSchema": {
            "type": "object", "properties": {"wait": {"type": "boolean"}}}},
            {"name": "retarget_project", "description": "Change project", "inputSchema": {"type": "object"}}]}
    elif method == "resources/list":
        result = {"resources": [{"uri": "fixture://source", "name": "source"}]}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "source"}]}
    elif method == "ping":
        result = {}
    else:
        if params.get("arguments", {}).get("wait"):
            (root / "entered").touch()
            deadline = time.monotonic() + 20
            while not (root / "release").exists() and time.monotonic() < deadline:
                time.sleep(.01)
        text = json.dumps({"source": (root / "symbols.txt").read_text(), "root": str(root), "cwd": os.getcwd()})
        if method == "resources/read":
            result = {"contents": [{"uri": "fixture://source", "text": text}]}
        elif method == "prompts/get":
            result = {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        else:
            result = {"content": [{"type": "text", "text": text}]}
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
