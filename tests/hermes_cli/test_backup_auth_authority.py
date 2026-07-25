from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
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


def _assert_transition_gate_held(auth_mod) -> None:
    competing_result: list[str] = []

    def compete() -> None:
        try:
            with auth_mod._auth_transition_lock(timeout_seconds=0.1):
                competing_result.append("acquired")
        except TimeoutError:
            competing_result.append("blocked")

    thread = threading.Thread(target=compete)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert competing_result == ["blocked"]


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
    (backup_home / "config.yaml").write_text(
        "auth:\n  authority: shared\nmarker: archived\n", encoding="utf-8"
    )
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
    (backup_home / "config.yaml").write_text(
        "auth:\n  authority: shared\nmarker: destination\n", encoding="utf-8"
    )
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
    assert "marker: archived" in (backup_home / "config.yaml").read_text()


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


@pytest.mark.parametrize("failure_phase", ["planned", "backed_up", "auth_written"])
def test_auth_restore_journal_recovers_interrupted_restore_by_rollback(
    backup_home, failure_phase: str
):
    import hermes_cli.backup as backup_mod
    from hermes_cli.auth_authority import AuthAuthorityConfigError, resolve_auth_authority

    old_auth = (backup_home / "auth.json").read_bytes()
    old_config = (backup_home / "config.yaml").read_bytes()
    restored_auth = b'{"providers":{"restored":{"token":"new"}}}'

    def crash(phase: str) -> None:
        if phase == failure_phase:
            raise SystemExit(f"crashed at {phase}")

    with pytest.raises(SystemExit, match="crashed"):
        backup_mod._restore_auth_transactionally(
            restored_auth,
            "restore-profile",
            config_home=backup_home,
            failure_injector=crash,
        )

    with pytest.raises(AuthAuthorityConfigError, match="incomplete restore"):
        resolve_auth_authority(profile_home=backup_home, shared_root=backup_home)

    expected = "aborted" if failure_phase in {"planned", "backed_up"} else "rolled_back"
    assert backup_mod._recover_incomplete_auth_restores() == [expected]
    assert (backup_home / "auth.json").read_bytes() == old_auth
    assert (backup_home / "config.yaml").read_bytes() == old_config
    resolve_auth_authority(profile_home=backup_home, shared_root=backup_home)


def test_auth_restore_journal_commits_forward_after_both_writes_land(backup_home):
    import hermes_cli.backup as backup_mod

    restored_auth = b'{"providers":{"restored":{"token":"new"}}}'

    def crash(phase: str) -> None:
        if phase == "config_written":
            raise SystemExit("crashed after config write")

    with pytest.raises(SystemExit, match="config write"):
        backup_mod._restore_auth_transactionally(
            restored_auth,
            "restore-profile",
            config_home=backup_home,
            failure_injector=crash,
        )

    assert backup_mod._recover_incomplete_auth_restores() == ["committed"]
    assert (backup_home / "auth.json").read_bytes() == restored_auth
    assert "authority: profile" in (backup_home / "config.yaml").read_text()
    journal_path = next(
        (backup_home / "state-snapshots" / "auth-restores" / "journals").glob(
            "*.json"
        )
    )
    assert json.loads(journal_path.read_text())["phase"] == "committed"


def test_auth_restore_recovery_preserves_unrecognized_external_change(backup_home):
    import hermes_cli.backup as backup_mod

    restored_auth = b'{"providers":{"restored":{"token":"new"}}}'

    def crash(phase: str) -> None:
        if phase == "auth_written":
            raise SystemExit("crashed after auth write")

    with pytest.raises(SystemExit):
        backup_mod._restore_auth_transactionally(
            restored_auth,
            "restore-shared",
            config_home=backup_home,
            failure_injector=crash,
        )
    external = b'{"providers":{"external":{"token":"preserve"}}}'
    (backup_home / "auth.json").write_bytes(external)

    with pytest.raises(RuntimeError, match="manual recovery required"):
        backup_mod._recover_incomplete_auth_restores()

    assert (backup_home / "auth.json").read_bytes() == external
    journal_path = next(
        (backup_home / "state-snapshots" / "auth-restores" / "journals").glob(
            "*.json"
        )
    )
    journal = json.loads(journal_path.read_text())
    assert journal["phase"] == "manual_required"
    assert journal["reason"] == "restore_state_changed"


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


def test_quick_auth_restore_rolls_back_destination_config_on_commit_failure(
    backup_home, tmp_path, monkeypatch
):
    import hermes_cli.backup as backup_mod

    passphrase = tmp_path / "quick-rollback-passphrase"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    (backup_home / "config.yaml").write_text(
        "auth:\n  authority: shared\nmarker: snapshot\n", encoding="utf-8"
    )
    snapshot_id = backup_mod.create_quick_snapshot(
        label="auth-rollback",
        hermes_home=backup_home,
        auth_mode="include-encrypted",
        auth_passphrase_file=str(passphrase),
    )
    assert snapshot_id is not None

    old_auth = b'{"providers":{"old":{}}}'
    old_config = b"auth:\n  authority: shared\nmarker: destination\n"
    (backup_home / "auth.json").write_bytes(old_auth)
    (backup_home / "config.yaml").write_bytes(old_config)
    real_write = backup_mod._atomic_private_write
    failed = False

    def fail_config(path, raw):
        nonlocal failed
        real_write(path, raw)
        if Path(path).name == "config.yaml" and not failed:
            failed = True
            raise OSError("forced config commit failure")

    monkeypatch.setattr(backup_mod, "_atomic_private_write", fail_config)

    assert backup_mod.restore_quick_snapshot(
        snapshot_id,
        hermes_home=backup_home,
        include_auth=True,
        auth_action="restore-shared",
        auth_passphrase_file=str(passphrase),
    ) is False
    assert (backup_home / "auth.json").read_bytes() == old_auth
    assert (backup_home / "config.yaml").read_bytes() == old_config


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


def test_auth_restore_rechecks_quiescence_under_transition_gate_before_mutation(
    backup_home, monkeypatch
):
    import hermes_cli.auth as auth_mod
    import hermes_cli.backup as backup_mod

    passphrase = backup_home.parent / "passphrase-racing-gateway"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    backup_mod.run_backup(
        _backup_args(
            backup_home,
            auth_mode="include-encrypted",
            auth_passphrase_file=str(passphrase),
        )
    )
    archive = backup_home / "backups.zip"
    old_auth = b'{"providers":{"destination":{}}}'
    old_config = b"auth:\n  authority: shared\nmarker: destination\n"
    (backup_home / "auth.json").write_bytes(old_auth)
    (backup_home / "config.yaml").write_bytes(old_config)
    checks = 0
    store_lock_held = False
    real_store_locks = auth_mod._auth_store_locks

    @contextmanager
    def tracked_store_locks(*args, **kwargs):
        nonlocal store_lock_held
        with real_store_locks(*args, **kwargs) as locked:
            store_lock_held = True
            try:
                yield locked
            finally:
                store_lock_held = False

    monkeypatch.setattr(auth_mod, "_auth_store_locks", tracked_store_locks)

    def gateway_starts_after_preflight(_home, _auth_action):
        nonlocal checks
        checks += 1
        if checks == 2:
            assert store_lock_held
            _assert_transition_gate_held(auth_mod)
            raise RuntimeError("gateway started after preflight")

    monkeypatch.setattr(
        backup_mod,
        "_assert_auth_restore_quiescent",
        gateway_starts_after_preflight,
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

    assert checks == 2
    assert (backup_home / "auth.json").read_bytes() == old_auth
    assert (backup_home / "config.yaml").read_bytes() == old_config


@pytest.mark.parametrize(
    "malformed_store",
    [
        {"providers": [], "credential_pool": {}},
        {"providers": {}, "credential_pool": []},
    ],
)
def test_encrypted_restore_rejects_malformed_auth_sections_before_writes(
    backup_home, malformed_store
):
    import hermes_cli.backup as backup_mod

    passphrase = backup_home.parent / "passphrase-malformed-sections"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    (backup_home / "auth.json").write_text(
        json.dumps(malformed_store), encoding="utf-8"
    )
    (backup_home / "MEMORY.md").write_text("archive value", encoding="utf-8")
    backup_mod.run_backup(
        _backup_args(
            backup_home,
            auth_mode="include-encrypted",
            auth_passphrase_file=str(passphrase),
        )
    )
    archive = backup_home / "backups.zip"
    old_auth = b'{"providers":{"destination":{}}}'
    old_config = b"auth:\n  authority: shared\nmarker: destination\n"
    (backup_home / "auth.json").write_bytes(old_auth)
    (backup_home / "config.yaml").write_bytes(old_config)
    (backup_home / "MEMORY.md").write_text("destination value", encoding="utf-8")

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


def test_auth_restore_recovery_validates_all_backups_before_mutating_targets(
    backup_home,
):
    import hermes_cli.backup as backup_mod

    restored_auth = b'{"providers":{"restored":{"token":"new"}}}'

    def crash(phase: str) -> None:
        if phase == "auth_written":
            raise SystemExit("simulated process death")

    with pytest.raises(SystemExit, match="simulated process death"):
        backup_mod._restore_auth_transactionally(
            restored_auth,
            "restore-profile",
            config_home=backup_home,
            failure_injector=crash,
        )

    journal_path = next(
        (backup_home / "state-snapshots" / "auth-restores" / "journals").glob(
            "*.json"
        )
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    config_before_recovery = (backup_home / "config.yaml").read_bytes()
    auth_before_recovery = (backup_home / "auth.json").read_bytes()
    (Path(journal["current_dir"]) / "config.yaml").write_bytes(b"corrupt backup")

    with pytest.raises(RuntimeError, match="backup verification failed"):
        backup_mod._recover_incomplete_auth_restores()

    assert (backup_home / "auth.json").read_bytes() == auth_before_recovery
    assert (backup_home / "config.yaml").read_bytes() == config_before_recovery
    assert json.loads(journal_path.read_text(encoding="utf-8"))["phase"] == (
        "manual_required"
    )


def test_auth_restore_holds_transition_gate_through_target_write(
    backup_home, monkeypatch
):
    import hermes_cli.auth as auth_mod
    import hermes_cli.backup as backup_mod

    restored_auth = b'{"providers":{"restored":{"token":"new"}}}'
    real_write = backup_mod._atomic_private_write
    competing_result: list[str] = []
    checked = False

    def checked_write(path: Path, raw: bytes) -> None:
        nonlocal checked
        if Path(path) == backup_home / "auth.json" and not checked:
            checked = True

            def compete() -> None:
                try:
                    with auth_mod._auth_transition_lock(timeout_seconds=0.1):
                        competing_result.append("acquired")
                except TimeoutError:
                    competing_result.append("blocked")

            thread = threading.Thread(target=compete)
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive()
        real_write(path, raw)

    monkeypatch.setattr(backup_mod, "_atomic_private_write", checked_write)

    backup_mod._restore_auth_transactionally(
        restored_auth,
        "restore-profile",
        config_home=backup_home,
    )

    assert competing_result == ["blocked"]


def test_encrypted_backup_snapshot_serializes_against_auth_writer(
    backup_home, monkeypatch
):
    import hermes_cli.auth as auth_mod
    import hermes_cli.backup as backup_mod

    passphrase = backup_home.parent / "passphrase-concurrent-writer"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    before = b'{"providers":{"nous":{"generation":"before-backup"}}}'
    (backup_home / "auth.json").write_bytes(before)
    writer_timed_out = threading.Event()
    retry_writer = threading.Event()
    writer_finished = threading.Event()
    captured: list[bytes] = []

    def writer() -> None:
        try:
            with auth_mod._auth_store_lock(timeout_seconds=0.1):
                pytest.fail("writer acquired auth lock during encrypted snapshot")
        except TimeoutError:
            writer_timed_out.set()
        assert retry_writer.wait(5)
        with auth_mod._auth_store_lock(timeout_seconds=2):
            store = auth_mod._load_auth_store()
            store["providers"]["nous"]["generation"] = "after-backup"
            auth_mod._save_auth_store(store)
        writer_finished.set()

    worker = threading.Thread(target=writer)

    def controlled_encrypt(raw: bytes, _passphrase: str):
        captured.append(raw)
        worker.start()
        assert writer_timed_out.wait(3)
        return b"test-encrypted-envelope", {"version": 2, "sha256": "test"}

    monkeypatch.setattr(backup_mod, "_encrypt_auth", controlled_encrypt)
    backup_mod.run_backup(
        _backup_args(
            backup_home,
            auth_mode="include-encrypted",
            auth_passphrase_file=str(passphrase),
        )
    )
    retry_writer.set()
    worker.join(5)

    assert not worker.is_alive()
    assert writer_finished.is_set()
    assert captured == [before]
    final = json.loads((backup_home / "auth.json").read_text(encoding="utf-8"))
    assert final["providers"]["nous"]["generation"] == "after-backup"
    with zipfile.ZipFile(backup_home / "backups.zip") as zf:
        assert zf.read("_auth/authority.enc") == b"test-encrypted-envelope"
