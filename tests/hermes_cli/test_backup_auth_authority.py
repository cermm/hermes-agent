from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest


@pytest.fixture
def backup_home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    (root / "config.yaml").write_text("auth:\n  authority: shared\n", encoding="utf-8")
    (root / "auth.json").write_text(
        json.dumps({"providers": {"nous": {"access_token": "super-secret"}}}),
        encoding="utf-8",
    )
    return root


def _backup_args(root: Path, **overrides):
    values = {
        "output": str(root / "backups"),
        "label": None,
        "auth_mode": "exclude",
        "auth_passphrase_file": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_full_backup_excludes_auth_by_default(backup_home):
    from hermes_cli.backup import run_backup

    run_backup(_backup_args(backup_home))
    archive = backup_home / "backups.zip"
    with zipfile.ZipFile(archive) as zf:
        assert not any(
            Path(name).name in {"auth.json", "auth.lock"}
            for name in zf.namelist()
        )
        payload = b"".join(zf.read(name) for name in zf.namelist())
        assert b"super-secret" not in payload


def test_full_backup_encrypts_and_restores_to_explicit_authority(backup_home):
    from hermes_cli.backup import run_backup, run_import

    passphrase = backup_home.parent / "passphrase"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    run_backup(
        _backup_args(
            backup_home,
            auth_mode="include-encrypted",
            auth_passphrase_file=str(passphrase),
        )
    )
    archive = backup_home / "backups.zip"
    with zipfile.ZipFile(archive) as zf:
        assert "_auth/manifest.json" in zf.namelist()
        assert "_auth/authority.enc" in zf.namelist()
        assert b"super-secret" not in zf.read("_auth/authority.enc")

    (backup_home / "auth.json").write_text('{"providers":{}}', encoding="utf-8")
    run_import(
        SimpleNamespace(
            zipfile=str(archive),
            force=True,
            clean=False,
            auth_action="restore-shared",
            auth_passphrase_file=str(passphrase),
        )
    )
    restored = json.loads((backup_home / "auth.json").read_text())
    assert restored["providers"]["nous"]["access_token"] == "super-secret"
    assert (backup_home / "auth.json").stat().st_mode & 0o777 == 0o600


def test_pre_update_backup_excludes_all_auth_stores(backup_home):
    from hermes_cli.backup import create_pre_update_backup

    profile = backup_home / "profiles" / "coder"
    profile.mkdir(parents=True)
    (profile / "auth.json").write_text('{"profile":"secret"}', encoding="utf-8")

    archive = create_pre_update_backup(backup_home)

    assert archive is not None
    with zipfile.ZipFile(archive) as zf:
        assert not any(
            Path(name).name in {"auth.json", "auth.lock"}
            for name in zf.namelist()
        )


def test_restore_rejects_archive_authority_topology_mismatch_before_writes(
    backup_home, monkeypatch
):
    import hermes_cli.backup as backup_mod

    passphrase = backup_home.parent / "passphrase-mismatch"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    profile = backup_home / "profiles" / "source"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "auth:\n  authority: profile\n", encoding="utf-8"
    )
    (profile / "auth.json").write_text(
        '{"providers":{"profile":{}}}', encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))
    backup_mod.run_backup(
        _backup_args(
            backup_home,
            auth_mode="include-encrypted",
            auth_passphrase_file=str(passphrase),
        )
    )
    archive = backup_home / "backups.zip"

    with pytest.raises(SystemExit):
        backup_mod.run_import(
            SimpleNamespace(
                zipfile=str(archive),
                force=True,
                clean=False,
                auth_action="restore-shared",
                auth_passphrase_file=str(passphrase),
            )
        )

    assert json.loads((backup_home / "auth.json").read_text())["providers"]["nous"]


def test_auth_restore_rolls_back_target_when_config_commit_fails(
    backup_home, monkeypatch
):
    import hermes_cli.backup as backup_mod

    passphrase = backup_home.parent / "passphrase-rollback"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    backup_mod.run_backup(
        _backup_args(
            backup_home,
            auth_mode="include-encrypted",
            auth_passphrase_file=str(passphrase),
        )
    )
    archive = backup_home / "backups.zip"
    old_auth = b'{"providers":{"old":{}}}'
    (backup_home / "auth.json").write_bytes(old_auth)
    real_write = getattr(backup_mod, "_atomic_private_write", None)

    def fail_config(path, raw):
        if Path(path).name == "config.yaml":
            raise OSError("forced config commit failure")
        assert real_write is not None
        return real_write(path, raw)

    monkeypatch.setattr(
        backup_mod, "_atomic_private_write", fail_config, raising=False
    )

    with pytest.raises(SystemExit):
        backup_mod.run_import(
            SimpleNamespace(
                zipfile=str(archive),
                force=True,
                clean=False,
                auth_action="restore-shared",
                auth_passphrase_file=str(passphrase),
            )
        )

    assert (backup_home / "auth.json").read_bytes() == old_auth


def test_full_backup_wrong_passphrase_and_legacy_auth_fail_closed(
    backup_home, tmp_path
):
    from hermes_cli.backup import run_backup, run_import

    good = backup_home.parent / "good-passphrase"
    good.write_text("correct horse battery staple", encoding="utf-8")
    bad = backup_home.parent / "bad-passphrase"
    bad.write_text("wrong", encoding="utf-8")
    run_backup(
        _backup_args(
            backup_home,
            auth_mode="include-encrypted",
            auth_passphrase_file=str(good),
        )
    )
    archive = backup_home / "backups.zip"
    with pytest.raises(SystemExit):
        run_import(
            SimpleNamespace(
                zipfile=str(archive),
                force=True,
                clean=False,
                auth_action="restore-shared",
                auth_passphrase_file=str(bad),
            )
        )

    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(legacy, "w") as zf:
        zf.writestr(".hermes/config.yaml", "model: {}\n")
        zf.writestr(".hermes/auth.json", '{"providers":{}}')
    with pytest.raises(SystemExit):
        run_import(
            SimpleNamespace(
                zipfile=str(legacy),
                force=True,
                clean=False,
                auth_action="skip",
                auth_passphrase_file=None,
            )
        )
