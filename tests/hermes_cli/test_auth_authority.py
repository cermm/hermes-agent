"""Behavior contracts for the canonical authentication authority resolver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def profile_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return {"root": root, "profile": profile}


def _write_config(profile: Path, auth: dict) -> None:
    (profile / "config.yaml").write_text(
        "auth:\n"
        + "\n".join(f"  {key}: {value}" for key, value in auth.items())
        + "\n",
        encoding="utf-8",
    )


def test_absent_config_defaults_to_shared_authority(profile_layout):
    from hermes_cli.auth_authority import resolve_auth_authority

    authority = resolve_auth_authority()

    assert authority.requested_mode == "shared"
    assert authority.effective_mode == "shared"
    assert authority.auth_path == profile_layout["root"] / "auth.json"
    assert authority.lock_path == profile_layout["root"] / "auth.lock"
    assert authority.profile_id == "coder"
    assert authority.legacy_compatibility is False


def test_absent_config_preserves_existing_profile_store(profile_layout):
    from hermes_cli.auth_authority import resolve_auth_authority

    (profile_layout["profile"] / "auth.json").write_text(
        json.dumps({"providers": {"nous": {"access_token": "legacy-token"}}}),
        encoding="utf-8",
    )

    authority = resolve_auth_authority()

    assert authority.requested_mode == "shared"
    assert authority.effective_mode == "profile"
    assert authority.auth_path == profile_layout["profile"] / "auth.json"
    assert authority.legacy_compatibility is True


def test_explicit_shared_mode_wins_and_reports_conflicting_profile_store(
    profile_layout,
):
    from hermes_cli.auth_authority import resolve_auth_authority

    (profile_layout["profile"] / "auth.json").write_text("{}", encoding="utf-8")
    _write_config(profile_layout["profile"], {"authority": "shared"})

    authority = resolve_auth_authority()

    assert authority.effective_mode == "shared"
    assert authority.auth_path == profile_layout["root"] / "auth.json"
    assert authority.conflicting_store == profile_layout["profile"] / "auth.json"


def test_profile_mode_uses_profile_store_and_authority_lock(profile_layout):
    from hermes_cli.auth_authority import resolve_auth_authority

    _write_config(profile_layout["profile"], {"authority": "profile"})

    authority = resolve_auth_authority()

    assert authority.effective_mode == "profile"
    assert authority.auth_path == profile_layout["profile"] / "auth.json"
    assert authority.lock_path == profile_layout["profile"] / "auth.lock"


@pytest.mark.parametrize(
    ("auth", "match"),
    [
        ({"authority": "unknown"}, "auth.authority"),
        ({"authority": "custom"}, "auth.authority"),
    ],
)
def test_invalid_authority_config_fails_closed(profile_layout, auth, match):
    from hermes_cli.auth_authority import (
        AuthAuthorityConfigError,
        resolve_auth_authority,
    )

    _write_config(profile_layout["profile"], auth)

    with pytest.raises(AuthAuthorityConfigError, match=match):
        resolve_auth_authority()


def test_auth_store_entrypoint_uses_canonical_authority(profile_layout):
    from hermes_cli.auth import _auth_file_path, _auth_lock_path

    _write_config(profile_layout["profile"], {"authority": "shared"})

    assert _auth_file_path() == profile_layout["root"] / "auth.json"
    assert _auth_lock_path() == profile_layout["root"] / "auth.lock"


def test_authority_status_is_redacted(profile_layout):
    from hermes_cli.auth_authority import auth_authority_status

    root = profile_layout["root"]
    token = "do-not-print-this-token"
    (root / "auth.json").write_text(
        json.dumps({"providers": {"nous": {"access_token": token}}})
    )

    rendered = json.dumps(auth_authority_status(), sort_keys=True)
    assert token not in rendered
    assert '"effective_mode": "shared"' in rendered
    assert '"path": "~/.hermes/auth.json"' in rendered
    assert str(root) not in rendered


def test_error_location_label_does_not_expose_operator_path(profile_layout):
    from hermes_cli.auth_authority import describe_auth_store

    label = describe_auth_store()

    assert label == "shared auth store (~/.hermes/auth.json)"
    assert str(profile_layout["root"]) not in label


def test_profile_location_label_is_normalized(profile_layout):
    from hermes_cli.auth_authority import describe_auth_store

    _write_config(profile_layout["profile"], {"authority": "profile"})

    assert describe_auth_store() == (
        "profile-local auth store (~/.hermes/profiles/coder/auth.json)"
    )


def test_auth_status_without_provider_reports_authority(profile_layout, capsys):
    from types import SimpleNamespace

    from hermes_cli.auth_commands import auth_status_command

    auth_status_command(SimpleNamespace(provider=None))
    output = capsys.readouterr().out
    assert "Authentication authority" in output
    assert "mode: shared" in output
    assert "do-not-print-this-token" not in output


def test_auth_status_all_profiles_reports_redacted_topology(profile_layout, capsys):
    from types import SimpleNamespace

    from hermes_cli.auth_commands import auth_status_command

    isolated = profile_layout["root"] / "profiles" / "isolated"
    isolated.mkdir(parents=True)
    _write_config(isolated, {"authority": "profile"})
    secret = "all-profiles-must-not-print-this-token"
    (isolated / "auth.json").write_text(
        json.dumps({"providers": {"nous": {"access_token": secret}}})
    )

    auth_status_command(SimpleNamespace(provider=None, all_profiles=True))
    output = capsys.readouterr().out

    assert "default: mode=shared" in output
    assert "coder: mode=shared" in output
    assert "isolated: mode=profile" in output
    assert secret not in output


def test_incomplete_restore_skips_unstatable_journal_candidate(profile_layout):
    from hermes_cli.auth_authority import incomplete_auth_restore

    journals = (
        profile_layout["root"]
        / "state-snapshots"
        / "auth-restores"
        / "journals"
    )
    journals.mkdir(parents=True)
    (journals / "valid.json").write_text(
        json.dumps({"operation_id": "valid-operation", "phase": "auth_written"}),
        encoding="utf-8",
    )
    (journals / "dangling.json").symlink_to(journals / "missing-target")

    assert incomplete_auth_restore(profile_layout["root"]) == {
        "operation_id": "valid-operation",
        "phase": "auth_written",
    }


@pytest.mark.parametrize("mode", ["shared", "profile"])
def test_authority_rejects_auth_store_symlinks(profile_layout, mode):
    from hermes_cli.auth_authority import (
        AuthAuthorityConfigError,
        resolve_auth_authority,
    )

    _write_config(profile_layout["profile"], {"authority": mode})
    outside = profile_layout["root"].parent / f"outside-{mode}.json"
    outside.write_text("{}", encoding="utf-8")
    target_home = (
        profile_layout["root"] if mode == "shared" else profile_layout["profile"]
    )
    (target_home / "auth.json").symlink_to(outside)

    with pytest.raises(AuthAuthorityConfigError, match="must not be a symlink"):
        resolve_auth_authority()
