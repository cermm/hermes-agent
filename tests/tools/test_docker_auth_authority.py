from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "docker_auth_authority.py"
_SPEC = importlib.util.spec_from_file_location("docker_auth_authority", _SCRIPT)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_shared_profile_resolves_root_store(tmp_path: Path) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("auth:\n  authority: shared\n", encoding="utf-8")

    result = _MOD.resolve_auth_authority(str(profile))

    assert result["authority"] == "shared"
    assert result["auth_path"] == str(root / "auth.json")


def test_absent_authority_preserves_existing_profile_local_store(tmp_path: Path) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "auth.json").write_text("{}", encoding="utf-8")

    result = _MOD.resolve_auth_authority(str(profile))

    assert result["authority"] == "profile"
    assert result["legacy_compatibility"] is True
    assert result["auth_path"] == str(profile / "auth.json")


def test_invalid_authority_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("auth:\n  authority: elsewhere\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid auth.authority"):
        _MOD.resolve_auth_authority(str(home))


def test_cli_emits_machine_readable_result(tmp_path: Path, capsys, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr("sys.argv", [str(_SCRIPT), str(home)])

    assert _MOD.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["auth_path"] == str(home / "auth.json")


def test_update_uses_canonical_shared_authority(tmp_path: Path) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("auth:\n  authority: shared\n", encoding="utf-8")
    shared = root / "auth.json"
    shared.write_text('{"providers": {"nous": {"status": "expired"}}}', encoding="utf-8")

    def refresh(store: dict) -> tuple[str, dict | None]:
        store["providers"]["nous"] = {"status": "valid"}
        return "reseeded", store

    assert _MOD.update_auth_store(profile, refresh) == "reseeded"
    assert json.loads(shared.read_text(encoding="utf-8"))["providers"]["nous"] == {"status": "valid"}
    assert not (profile / "auth.json").exists()


def test_update_rejects_target_symlink(tmp_path: Path) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("auth:\n  authority: shared\n", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (root / "auth.json").symlink_to(outside)

    with pytest.raises(RuntimeError, match="symlink"):
        _MOD.update_auth_store(profile, lambda store: ("unchanged", None))


def test_internal_authority_bridge_must_match_contained_configured_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "auth:\n  authority: profile\n", encoding="utf-8"
    )
    expected = profile / "auth.json"

    monkeypatch.setenv("HERMES_INTERNAL_AUTHORITY_PATH", str(expected))
    assert _MOD.resolve_auth_authority(str(profile))["auth_path"] == str(expected)

    monkeypatch.setenv("HERMES_INTERNAL_AUTHORITY_PATH", str(root / "auth.json"))
    with pytest.raises(ValueError, match="does not match"):
        _MOD.resolve_auth_authority(str(profile))

    monkeypatch.setenv(
        "HERMES_INTERNAL_AUTHORITY_PATH", str(tmp_path / "outside" / "auth.json")
    )
    with pytest.raises(ValueError, match="inside the Hermes root"):
        _MOD.resolve_auth_authority(str(profile))


def test_update_aborts_if_authority_cuts_over_while_waiting_for_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    config = profile / "config.yaml"
    config.write_text("auth:\n  authority: shared\n", encoding="utf-8")
    shared = root / "auth.json"
    shared.write_text('{"providers": {}}', encoding="utf-8")
    real_flock = _MOD.fcntl.flock
    cut_over = False

    def flock_then_cut_over(fd: int, operation: int) -> None:
        nonlocal cut_over
        real_flock(fd, operation)
        if operation == _MOD.fcntl.LOCK_EX and not cut_over:
            cut_over = True
            config.write_text("auth:\n  authority: profile\n", encoding="utf-8")

    monkeypatch.setattr(_MOD.fcntl, "flock", flock_then_cut_over)

    with pytest.raises(RuntimeError, match="changed while waiting"):
        _MOD.update_auth_store(
            profile, lambda store: ("written", {"providers": {"new": {}}})
        )

    assert json.loads(shared.read_text(encoding="utf-8")) == {"providers": {}}
    assert not (profile / "auth.json").exists()


def test_temporary_replace_failure_preserves_complete_previous_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text('{"providers": {"current": {}}}', encoding="utf-8")

    def fail_replace(_source, _destination) -> None:
        raise OSError("temporary filesystem failure")

    monkeypatch.setattr(_MOD.os, "replace", fail_replace)
    with pytest.raises(OSError, match="temporary filesystem failure"):
        _MOD.update_auth_store(
            home, lambda store: ("written", {"providers": {"replacement": {}}})
        )

    assert json.loads(auth.read_text(encoding="utf-8")) == {
        "providers": {"current": {}}
    }
    assert not list(home.glob(".auth-update-*"))


@pytest.mark.parametrize(
    "config_text",
    [
        "auth: {authority: profile}\n",
        "{auth: {authority: profile}}\n",
    ],
)
def test_inline_yaml_authority_matches_block_yaml(
    tmp_path: Path, config_text: str
) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(config_text, encoding="utf-8")

    result = _MOD.resolve_auth_authority(str(profile))

    assert result["authority"] == "profile"
    assert result["auth_path"] == str(profile / "auth.json")


@pytest.mark.parametrize(
    "config_text",
    [
        "auth: []\n",
        "auth: {authority: [profile]}\n",
        "auth: {authority: profile\n",
    ],
)
def test_malformed_authority_yaml_fails_closed(
    tmp_path: Path, config_text: str
) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(config_text, encoding="utf-8")

    with pytest.raises(RuntimeError, match="auth authority config"):
        _MOD.resolve_auth_authority(str(profile))
