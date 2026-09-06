"""Current runtime owners exercised against isolated shared-auth files and CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


def _profile(tmp_path, monkeypatch):
    root = tmp_path / "fleet"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return root, profile


def test_photon_logout_pins_original_authority(tmp_path, monkeypatch):
    root, profile = _profile(tmp_path, monkeypatch)
    (profile / "config.yaml").write_text("auth:\n  authority: profile\n")
    (profile / "auth.json").write_text(json.dumps({"credential_pool": {"photon": [{"access_token": "local"}]}}))
    (root / "auth.json").write_text(json.dumps({"credential_pool": {"openai": [{"access_token": "shared"}]}}))
    from plugins.platforms.photon import auth as photon

    original_load = photon._load_auth
    def load_then_change_config():
        loaded = original_load()
        (profile / "config.yaml").write_text("auth:\n  authority: shared\n")
        return loaded
    monkeypatch.setattr(photon, "_load_auth", load_then_change_config)
    original_root = (root / "auth.json").read_bytes()
    photon.clear_photon_token()
    assert (root / "auth.json").read_bytes() == original_root
    assert json.loads((profile / "auth.json").read_text())["credential_pool"]["photon"] == []


def test_transaction_pin_follows_profile_context_and_restores_outer(tmp_path, monkeypatch):
    root, profile = _profile(tmp_path, monkeypatch)
    sibling = root / "profiles" / "other"
    sibling.mkdir()
    for home in (profile, sibling):
        (home / "config.yaml").write_text("auth:\n  authority: profile\n")
    from hermes_cli.auth import _auth_store_lock, _auth_file_path
    from hermes_cli.auth_authority import get_auth_store_path, get_auth_lock_path
    from agent.auxiliary_client import _auth_json_path
    from tools.managed_tool_gateway import auth_json_path
    from agent.anthropic_credentials import _get_hermes_oauth_file

    with _auth_store_lock():
        (profile / "config.yaml").write_text("auth:\n  authority: shared\n")
        assert get_auth_store_path() == _auth_json_path() == auth_json_path() == profile / "auth.json"
        assert get_auth_lock_path() == profile / "auth.lock"
        assert _get_hermes_oauth_file() == profile / ".anthropic_oauth.json"
        monkeypatch.setenv("HERMES_HOME", str(sibling))
        assert _auth_file_path() == sibling / "auth.json"
        with _auth_store_lock():
            assert get_auth_store_path() == sibling / "auth.json"
        monkeypatch.setenv("HERMES_HOME", str(profile))
        assert _auth_file_path() == profile / "auth.json"
    assert get_auth_store_path() == root / "auth.json"


def test_actual_cli_migrates_bom_store_reports_profiles_and_rolls_back(tmp_path, monkeypatch):
    root, profile = _profile(tmp_path, monkeypatch)
    original = b'\xef\xbb\xbf' + json.dumps({"providers": {"openai-codex": {"access_token": "fake-private-token"}}}).encode()
    (profile / "auth.json").write_bytes(original)
    monkeypatch.setenv("HERMES_HOME", str(root))
    def cli(*args):
        result = subprocess.run([sys.executable, "-m", "hermes_cli.main", "auth", *args],
                                cwd=Path(__file__).resolve().parents[2], env=os.environ.copy(),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr + result.stdout
        assert "fake-private-token" not in result.stdout + result.stderr
        return result.stdout
    assert "worker: mode=profile" in cli("status", "--all-profiles")
    output = cli("migrate-shared", "--profile", "worker", "--dry-run")
    plan_id = re.search(r"plan_id: (\w+)", output).group(1)
    digest = re.search(r"plan_digest: (\w+)", output).group(1)
    assert not (root / "auth.json").exists()
    cli("migrate-shared", "--apply", "--plan-id", plan_id, "--plan-digest", digest, "--conflict-policy", "abort")
    assert "worker: mode=shared" in cli("status", "--all-profiles")
    assert json.loads((root / "auth.json").read_text())["providers"]["openai-codex"]["access_token"] == "fake-private-token"
    assert (root / "auth.json").stat().st_mode & 0o777 == 0o600
    cli("migrate-shared", "--rollback", "--plan-id", plan_id)
    assert not (root / "auth.json").exists()
    assert (profile / "auth.json").read_bytes() == original
    assert not (profile / "config.yaml").exists()


@pytest.mark.parametrize("phase", ["target_write_pending", "target_written", "profile_written"])
def test_interrupted_migration_blocks_then_recovers_without_losing_bom(tmp_path, monkeypatch, phase):
    root, profile = _profile(tmp_path, monkeypatch)
    original = b'\xef\xbb\xbf' + json.dumps({"providers": {"nous": {"access_token": "fake"}}}).encode()
    (profile / "auth.json").write_bytes(original)
    monkeypatch.setenv("HERMES_HOME", str(root))
    from hermes_cli.auth_authority import get_auth_store_path, AuthAuthorityConfigError
    from hermes_cli.auth_migration import plan_shared_migration, apply_shared_migration, recover_shared_migration

    plan = plan_shared_migration(all_profiles=True)
    def interrupt(checkpoint):
        if checkpoint == phase:
            raise RuntimeError("simulated crash")
    with pytest.raises(RuntimeError, match="simulated crash"):
        apply_shared_migration(plan_id=plan.plan_id, plan_digest=plan.plan_digest,
                               conflict_policy="abort", failure_injector=interrupt)
    with pytest.raises(AuthAuthorityConfigError, match="incomplete migration"):
        get_auth_store_path()
    assert recover_shared_migration(plan_id=plan.plan_id) in {"rolled_back", "aborted"}
    assert not (root / "auth.json").exists()
    assert not (profile / "config.yaml").exists()
    assert (profile / "auth.json").read_bytes() == original


def test_shared_authority_serializes_real_profile_writer_processes(tmp_path, monkeypatch):
    root, profile = _profile(tmp_path, monkeypatch)
    sibling = root / "profiles" / "sibling"
    sibling.mkdir()
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store

    code = '''from hermes_cli.auth import write_credential_pool
print("ready", flush=True)
write_credential_pool("openai", [{"id": "sibling", "source": "manual", "access_token": "fake"}])
print("committed", flush=True)
'''
    env = dict(os.environ, HERMES_HOME=str(sibling))
    child = None
    try:
        with _auth_store_lock():
            store = _load_auth_store()
            child = subprocess.Popen([sys.executable, "-c", code], env=env,
                                     cwd=Path(__file__).resolve().parents[2],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=0.3)
            store["providers"]["nous"] = {"access_token": "parent-fake"}
            _save_auth_store(store)
        output, error = child.communicate(timeout=10)
        assert child.returncode == 0 and "committed" in output, error
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()
    result = json.loads((root / "auth.json").read_text())
    assert result["providers"]["nous"]["access_token"] == "parent-fake"
    assert result["credential_pool"]["openai"][0]["id"] == "sibling"
    assert not (profile / "auth.json").exists() and not (sibling / "auth.json").exists()
