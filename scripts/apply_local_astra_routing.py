"""Apply the versioned local model policy without committing private configuration."""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.util import load_yaml_guess_indent

BEGIN = "[MODEL_ROUTING_POLICY]"
END = "[/MODEL_ROUTING_POLICY]"


def merge(target, updates):
    for key, value in updates.items():
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            merge(target[key], value)
        else:
            target[key] = value


def render(source, entry):
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 10000
    config, indent, offset = load_yaml_guess_indent(source, yaml=yaml)
    yaml.indent(mapping=2, sequence=indent or 2, offset=offset or 0)
    merge(config, entry["updates"])
    if prompt := entry.get("append_system_prompt"):
        agent = config.setdefault("agent", {})
        previous = str(agent.get("system_prompt") or "")
        if BEGIN in previous and END in previous:
            start = previous.index(BEGIN)
            end = previous.index(END, start) + len(END)
            previous = previous[:start] + previous[end:]
        agent["system_prompt"] = previous.strip() + "\n\n" + BEGIN + "\n" + prompt + "\n" + END
    output = io.StringIO()
    yaml.dump(config, output)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    policy = json.loads((Path(__file__).resolve().parents[1] / "config/local-astra-routing.json").read_text())
    changes = []
    for entry in policy["entries"]:
        profile = entry["profile"]
        relative = Path("config.yaml") if profile == "default" else Path("profiles") / profile / "config.yaml"
        path = args.home / relative
        source = path.read_text()
        updated = render(source, {**entry, "append_system_prompt": policy["routing_prompt"]})
        if updated != source:
            changes.append((relative, path, updated))
        print(profile, "->", entry["updates"]["model"]["default"])
    if not args.apply or not changes:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.home / "backups" / ("astra-routing-" + stamp)
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    for relative, path, updated in changes:
        saved = backup / relative
        saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(path, saved)
        fd, temporary = tempfile.mkstemp(prefix=".astra-routing-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(updated)
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print("Private configuration backup:", backup)


if __name__ == "__main__":
    main()
