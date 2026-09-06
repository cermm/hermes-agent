import importlib.util
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml
import pytest

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
    policy = json.loads((ROOT / "config/local-astra-routing.json").read_text(encoding="utf-8"))
    ladder = [tier["model"] for tier in policy["tiers"]]
    for entry in policy_script.compile_policy(policy):
        updates = entry["updates"]
        if entry["profile"] == "default":
            route = [updates["delegation"]["model"]] + [x["model"] for x in updates["delegation"]["fallback_providers"]]
        else:
            route = [updates["model"]["default"]] + [x["model"] for x in updates["fallback_providers"]]
        assert route == ladder[ladder.index(route[0]):]


def test_compiled_routes_preserve_every_previous_profile_and_render_independently():
    policy = json.loads((ROOT / "config/local-astra-routing.json").read_text(encoding="utf-8"))
    before = copy.deepcopy(policy)
    entries = policy_script.compile_policy(policy)
    expected = json.loads((ROOT / "tests/fixtures/local-routing-v1-fingerprints.json").read_text(encoding="utf-8"))
    assert {entry['profile']: hashlib.sha256(json.dumps(entry['updates'], sort_keys=True).encode()).hexdigest() for entry in entries} == expected
    for entry in entries:
        source = '# preserved comment\nmodel:\n  default: previous\n  provider: pinned-provider\n  api_key: fake-private-key\nagent:\n  max_turns: 42\n  system_prompt: Original instructions.\n'
        rendered = policy_script.render(source, {**entry, 'append_system_prompt': policy['routing_prompt']})
        assert policy_script.render(rendered, {**entry, 'append_system_prompt': policy['routing_prompt']}) == rendered
        observed = yaml.safe_load(rendered)
        assert observed['model']['api_key'] == 'fake-private-key'
        assert observed['agent']['max_turns'] == 42
        if entry['profile'] == 'default':
            assert observed['model']['provider'] == 'pinned-provider'
    assert policy == before
    entries[1]['updates']['delegation']['fallback_providers'].clear()
    assert entries[2]['updates']['delegation']['fallback_providers']


def test_one_tier_edit_updates_every_route_without_stale_copies():
    policy = json.loads((ROOT / "config/local-astra-routing.json").read_text(encoding="utf-8"))
    old_model = policy['tiers'][1]['model']
    policy['tiers'][1]['model'] = 'synthetic-replacement-model'
    entries = policy_script.compile_policy(policy)
    assert old_model not in json.dumps(entries)
    low = next(entry for entry in entries if entry['profile'] == 'builder-low')
    assert low['updates']['model']['default'] == 'synthetic-replacement-model'
    default = next(entry for entry in entries if entry['profile'] == 'default')
    assert default['updates']['delegation']['fallback_providers'][0]['model'] == 'synthetic-replacement-model'


def test_real_policy_command_plans_applies_and_preserves_private_backups(tmp_path):
    policy = json.loads((ROOT / "config/local-astra-routing.json").read_text(encoding="utf-8"))
    originals, expected = {}, {}
    for entry in policy_script.compile_policy(policy):
        relative = Path('config.yaml') if entry['profile'] == 'default' else Path('profiles') / entry['profile'] / 'config.yaml'
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        source = f'# {entry["profile"]} private fixture\nmodel:\n  default: previous\n  provider: pinned-provider\n  api_key: fake-key\nagent:\n  max_turns: 42\n  system_prompt: Preserve my instructions. Žluťoučký kůň 🙂\n'
        path.write_text(source, encoding="utf-8")
        path.chmod(0o600)
        originals[relative] = source
        expected[relative] = policy_script.render(source, {**entry, 'append_system_prompt': policy['routing_prompt']})
    command = [sys.executable, str(ROOT / 'scripts/apply_local_astra_routing.py'), '--home', str(tmp_path)]
    # File encoding must not depend on the invoking shell's locale.
    environment = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path),
                       PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C")
    def run(*args):
        result = subprocess.run([*command, *args], cwd=tmp_path, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        return result
    run()
    assert not (tmp_path / 'backups').exists()
    assert all((tmp_path / relative).read_text(encoding="utf-8") == source for relative, source in originals.items())
    run('--apply')
    backups = list((tmp_path / 'backups').iterdir())
    assert len(backups) == 1
    for relative, source in originals.items():
        path = tmp_path / relative
        assert path.read_text(encoding="utf-8") == expected[relative]
        assert (backups[0] / relative).read_text(encoding="utf-8") == source
        if os.name == 'posix':
            assert path.stat().st_mode & 0o777 == 0o600
            assert (backups[0] / relative).stat().st_mode & 0o777 == 0o600
    run('--apply')
    assert list((tmp_path / 'backups').iterdir()) == backups


@pytest.mark.parametrize('defect', ['duplicate_tier', 'unknown_tier', 'duplicate_profile', 'traversal_profile'])
def test_invalid_policy_is_rejected_before_rendering(defect):
    policy = json.loads((ROOT / "config/local-astra-routing.json").read_text(encoding="utf-8"))
    if defect == 'duplicate_tier':
        policy['tiers'].append(copy.deepcopy(policy['tiers'][0]))
    elif defect == 'unknown_tier':
        policy['entries'][0]['tier'] = 'absent'
    elif defect == 'duplicate_profile':
        policy['entries'].append(copy.deepcopy(policy['entries'][0]))
    else:
        policy['entries'][0]['profile'] = '../outside'
    with pytest.raises(ValueError):
        policy_script.compile_policy(policy)
