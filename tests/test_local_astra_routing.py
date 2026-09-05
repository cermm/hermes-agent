import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_astra_routing", ROOT / "scripts/apply_local_astra_routing.py")
policy_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy_script)


def test_policy_preserves_credentials_and_is_idempotent():
    source = "# keep\nmodel:\n  default: old\n  provider: openai-codex\n  api_key: private-test-value\nagent:\n  max_turns: 42\n  system_prompt: Original instructions.\n"
    entry = {"updates": {"model": {"default": "gpt-6-astra"}}, "append_system_prompt": "Policy instructions."}
    updated = policy_script.render(source, entry)
    data = yaml.safe_load(updated)
    assert data["model"]["api_key"] == "private-test-value"
    assert data["agent"]["max_turns"] == 42
    assert "Original instructions." in data["agent"]["system_prompt"]
    assert updated.startswith("# keep")
    assert policy_script.render(updated, entry) == updated


def test_worker_ladders_only_escalate_and_end_at_astra():
    ladder = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"]
    policy = json.loads((ROOT / "config/local-astra-routing.json").read_text())
    for entry in policy["entries"]:
        updates = entry["updates"]
        if entry["profile"] == "default":
            route = [updates["delegation"]["model"]] + [x["model"] for x in updates["delegation"]["fallback_providers"]]
        else:
            route = [updates["model"]["default"]] + [x["model"] for x in updates["fallback_providers"]]
        assert route == ladder[ladder.index(route[0]):]
