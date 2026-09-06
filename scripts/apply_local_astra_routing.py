"""Apply the versioned local model policy without committing private configuration."""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.util import load_yaml_guess_indent

BEGIN = "[MODEL_ROUTING_POLICY]"
END = "[/MODEL_ROUTING_POLICY]"

def compile_policy(policy):
    """Expand one ordered tier table into the existing render-entry contract."""
    if policy.get("version") != 2:
        raise ValueError("unsupported local routing policy version")
    tiers = policy.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise ValueError("routing tiers must be a nonempty ordered list")
    by_name, identities = {}, set()
    for position, tier in enumerate(tiers):
        if not isinstance(tier, dict) or set(tier) != {"name", "model", "provider"} or not all(isinstance(value, str) and value.strip() for value in tier.values()):
            raise ValueError("invalid routing tier")
        identity = (tier["provider"], tier["model"])
        if tier["name"] in by_name or identity in identities:
            raise ValueError("duplicate routing tier or model")
        by_name[tier["name"]] = position
        identities.add(identity)

    def route(name):
        if name not in by_name:
            raise ValueError(f"unknown routing tier: {name}")
        position = by_name[name]
        return {key: tiers[position][key] for key in ("provider", "model")}, [
            {key: tier[key] for key in ("provider", "model")} for tier in tiers[position + 1:]
        ]

    entries, profiles = [], set()
    allowed = {"profile", "tier", "reasoning_effort", "delegation_tier", "delegation_effort", "set_provider", "worker_fallbacks"}
    for entry in policy["entries"]:
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ValueError("invalid routing entry fields")
        profile = entry["profile"]
        if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", profile) or profile in profiles:
            raise ValueError("invalid or duplicate routing profile")
        profiles.add(profile)
        for key in ("set_provider", "worker_fallbacks"):
            if key in entry and not isinstance(entry[key], bool):
                raise ValueError(f"{key} must be boolean")
        primary, fallbacks = route(entry["tier"])
        delegate, delegate_fallbacks = route(entry.get("delegation_tier", entry["tier"]))
        updates = {"model": {"default": primary["model"]}}
        if entry.get("set_provider", True):
            updates["model"]["provider"] = primary["provider"]
        if "reasoning_effort" in entry:
            updates["agent"] = {"reasoning_effort": entry["reasoning_effort"]}
        if entry.get("worker_fallbacks", True):
            updates["fallback_providers"] = fallbacks
        updates["delegation"] = {**delegate, "reasoning_effort": entry.get("delegation_effort", "medium"),
                                 "fallback_providers": delegate_fallbacks, "escalate_on_validation_failure": True}
        entries.append({"profile": profile, "updates": updates})
    return entries


def merge(target, updates):
    for key, value in updates.items():
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


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
    policy = json.loads((Path(__file__).resolve().parents[1] / "config/local-astra-routing.json").read_text(encoding="utf-8"))
    changes = []
    for entry in compile_policy(policy):
        profile = entry["profile"]
        relative = Path("config.yaml") if profile == "default" else Path("profiles") / profile / "config.yaml"
        path = args.home / relative
        source = path.read_text(encoding="utf-8")
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
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(updated)
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print("Private configuration backup:", backup)


if __name__ == "__main__":
    main()
