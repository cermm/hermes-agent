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
