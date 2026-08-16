"""Focused shared-auth authority regressions for issue #380."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _reset_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))


def test_default_profile_uses_shared_auth_store_when_no_legacy_profile_store(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    _reset_home(monkeypatch, home)

    from hermes_cli.auth_authority import resolve_auth_authority

    authority = resolve_auth_authority()

    assert authority.effective_mode == "shared"
    assert authority.provenance == "shared-root"
    assert authority.auth_path == home / "auth.json"
    assert authority.lock_path == home / "auth.lock"


def test_named_profile_inherits_shared_store_by_default(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "builder-high"
    profile.mkdir(parents=True)
    _reset_home(monkeypatch, profile)

    from hermes_cli.auth_authority import resolve_auth_authority

    authority = resolve_auth_authority()

    assert authority.effective_mode == "shared"
    assert authority.profile_id == "builder-high"
    assert authority.auth_path == root / "auth.json"
    assert authority.lock_path == root / "auth.lock"


def test_existing_profile_auth_json_is_legacy_local_until_explicit_config(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "reviewer"
    profile.mkdir(parents=True)
    (root / "auth.json").write_text(json.dumps({"providers": {"nous": {}}}), encoding="utf-8")
    (profile / "auth.json").write_text(json.dumps({"providers": {"openai-codex": {}}}), encoding="utf-8")
    _reset_home(monkeypatch, profile)

    from hermes_cli.auth_authority import resolve_auth_authority

    authority = resolve_auth_authority()

    assert authority.effective_mode == "profile"
    assert authority.legacy_compatibility is True
    assert authority.provenance == "legacy-profile-store"
    assert authority.conflicting_store == root / "auth.json"


def test_explicit_profile_authority_marks_profile_local_override(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "researcher"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("auth:\n  authority: profile\n", encoding="utf-8")
    _reset_home(monkeypatch, profile)

    from hermes_cli.auth_authority import resolve_auth_authority

    authority = resolve_auth_authority()

    assert authority.effective_mode == "profile"
    assert authority.provenance == "profile:researcher"
    assert authority.auth_path == profile / "auth.json"


def test_auth_status_without_provider_reports_redacted_authority(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    home.mkdir()
    _reset_home(monkeypatch, home)

    from hermes_cli.auth_commands import auth_status_command

    auth_status_command(type("Args", (), {"provider": None, "all_profiles": False})())
    out = capsys.readouterr().out

    assert "Authentication authority" in out
    assert "mode: shared" in out
    assert "provenance: shared-root" in out
    assert "access_token" not in out
    assert "refresh_token" not in out


def test_profile_write_uses_shared_auth_store_by_default(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "builder-high"
    profile.mkdir(parents=True)
    _reset_home(monkeypatch, profile)

    from hermes_cli import auth

    assert auth._auth_file_path() == root / "auth.json"
    auth.write_credential_pool(
        "openai-codex",
        [
            {
                "id": "one",
                "label": "fake",
                "auth_type": "oauth",
                "source": "manual:device_code",
                "access_token": "fake-access",
                "refresh_token": "fake-refresh",
            }
        ],
    )

    assert (root / "auth.json").is_file()
    assert not (profile / "auth.json").exists()
    assert auth.read_credential_pool("openai-codex")[0]["id"] == "one"


def test_explicit_profile_authority_write_stays_profile_local(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "reviewer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("auth:\n  authority: profile\n", encoding="utf-8")
    _reset_home(monkeypatch, profile)

    from hermes_cli import auth

    assert auth._auth_file_path() == profile / "auth.json"


def test_auth_store_lock_pins_data_path_across_authority_transition(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "p1"
    profile.mkdir(parents=True)
    (profile / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {}}), encoding="utf-8"
    )
    _reset_home(monkeypatch, profile)

    from hermes_cli import auth

    with auth._auth_store_lock():
        assert auth._auth_file_path() == profile / "auth.json"
        (profile / "config.yaml").write_text("auth:\n  authority: shared\n", encoding="utf-8")
        auth.write_credential_pool(
            "openai-codex",
            [
                {
                    "id": "one",
                    "label": "test",
                    "auth_type": "oauth",
                    "source": "manual:device_code",
                    "access_token": "transition-token",
                }
            ],
        )

    profile_data = json.loads((profile / "auth.json").read_text(encoding="utf-8"))
    assert profile_data["credential_pool"]["openai-codex"][0]["id"] == "one"
    assert not (root / "auth.json").exists()
