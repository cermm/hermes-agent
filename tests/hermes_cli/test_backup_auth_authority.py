from __future__ import annotations

import json
import os
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
    old_config = b"auth:\n  authority: shared\noperator_marker: destination\n"
    (backup_home / "config.yaml").write_bytes(old_config)
    real_write = getattr(backup_mod, "_atomic_private_write", None)
    failed = False

    def fail_config(path, raw):
        nonlocal failed
        if Path(path).name == "config.yaml" and not failed:
            failed = True
            assert real_write is not None
            real_write(path, raw)
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
    assert (backup_home / "config.yaml").read_bytes() == old_config


def test_quick_auth_restore_requires_explicit_action_before_any_write(
    backup_home, tmp_path
):
    import hermes_cli.backup as backup_mod

    passphrase = tmp_path / "quick-passphrase"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    (backup_home / "config.yaml").write_text(
        "auth:\n  authority: shared\nmarker: snapshot\n", encoding="utf-8"
    )
    snapshot_id = backup_mod.create_quick_snapshot(
        label="auth-contract",
        hermes_home=backup_home,
        auth_mode="include-encrypted",
        auth_passphrase_file=str(passphrase),
    )
    assert snapshot_id is not None
    destination = b"auth:\n  authority: shared\nmarker: destination\n"
    (backup_home / "config.yaml").write_bytes(destination)

    assert backup_mod.restore_quick_snapshot(
        snapshot_id,
        hermes_home=backup_home,
        include_auth=True,
        auth_passphrase_file=str(passphrase),
    ) is False
    assert (backup_home / "config.yaml").read_bytes() == destination


def test_quick_auth_restore_rejects_live_gateway_before_any_write(
    backup_home, tmp_path, monkeypatch
):
    import hermes_cli.backup as backup_mod
    import gateway.status as gateway_status

    passphrase = tmp_path / "quick-live-passphrase"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    (backup_home / "config.yaml").write_text(
        "auth:\n  authority: shared\nmarker: snapshot\n", encoding="utf-8"
    )
    snapshot_id = backup_mod.create_quick_snapshot(
        label="live-gateway",
        hermes_home=backup_home,
        auth_mode="include-encrypted",
        auth_passphrase_file=str(passphrase),
    )
    assert snapshot_id is not None
    destination = b"auth:\n  authority: shared\nmarker: destination\n"
    (backup_home / "config.yaml").write_bytes(destination)
    monkeypatch.setattr(
        gateway_status,
        "get_running_pid",
        lambda *_args, **_kwargs: os.getpid(),
    )

    assert backup_mod.restore_quick_snapshot(
        snapshot_id,
        hermes_home=backup_home,
        include_auth=True,
        auth_action="restore-shared",
        auth_passphrase_file=str(passphrase),
    ) is False
    assert (backup_home / "config.yaml").read_bytes() == destination


def test_full_restore_refuses_running_shared_gateway_before_any_write(
    backup_home, monkeypatch
):
    import hermes_cli.backup as backup_mod
    from gateway import status as gateway_status

    passphrase = backup_home.parent / "passphrase-running"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    (backup_home / "MEMORY.md").write_text("archive value", encoding="utf-8")
    backup_mod.run_backup(
        _backup_args(
            backup_home,
            auth_mode="include-encrypted",
            auth_passphrase_file=str(passphrase),
        )
    )
    archive = backup_home / "backups.zip"
    (backup_home / "MEMORY.md").write_text("destination value", encoding="utf-8")
    monkeypatch.setattr(
        gateway_status,
        "get_running_pid",
        lambda *_args, **_kwargs: os.getpid(),
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

    assert (backup_home / "MEMORY.md").read_text() == "destination value"


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
