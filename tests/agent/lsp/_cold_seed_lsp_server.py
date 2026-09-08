"""Real stdio peer with independently gated first and recovery diagnostics."""
from _mock_lsp_server import read_message, write_message


def main():
    uri = None
    text = ""
    version = 0
    changes = []
    pulls = 0
    seeded = False
    released = False

    def send(method, params):
        write_message({"jsonrpc": "2.0", "method": method, "params": params})

    def diagnostics(content):
        return [{"range": {"start": {"line": 0, "character": 0},
                           "end": {"line": 0, "character": 1}},
                 "severity": 1, "code": "bad-assignment", "message": "bad assignment"}] if '"bad"' in content else []

    def publish(content, tag=None):
        params = {"uri": uri, "diagnostics": diagnostics(content)}
        if tag is not None:
            params["version"] = tag
        send("textDocument/publishDiagnostics", params)

    while msg := read_message():
        method, params = msg.get("method"), msg.get("params") or {}
        result = None
        if method == "initialize":
            result = {"capabilities": {"textDocumentSync": 2}}
        elif method == "textDocument/didOpen":
            doc = params["textDocument"]
            uri, text, version = doc["uri"], doc["text"], doc["version"]
        elif method == "textDocument/didChange":
            new_version = params["textDocument"]["version"]
            assert new_version > version
            text = params["contentChanges"][0]["text"]
            version = new_version
            changes.append({"version": version, "text": text})
            if seeded and released:
                publish(text, version)
        elif method == "textDocument/diagnostic":
            pulls += 1
            write_message({"jsonrpc": "2.0", "id": msg["id"],
                           "error": {"code": -32601, "message": "push only"}})
            continue
        elif method == "test/seed":
            seeded = True
            kind = params["kind"]
            content = 'count: int = 1\n' if kind.endswith("stale") else text
            if kind == "unversioned-stale-error":
                content = 'count: int = "bad"\n'
            tag = (0 if kind.endswith("stale") else version) if kind.startswith("versioned") else None
            if kind == "versioned-future":
                tag = version + 1
            elif kind == "versioned-bool":
                tag = True
            publish(content, tag)
        elif method == "test/publish":
            publish(params["text"], params.get("version"))
        elif method == "test/release":
            released = True
            publish(text, version)
        elif method == "test/state":
            result = {"changes": changes, "pulls": pulls, "version": version}
        elif method == "exit":
            return
        if "id" in msg:
            write_message({"jsonrpc": "2.0", "id": msg["id"], "result": result})


if __name__ == "__main__":
    main()
