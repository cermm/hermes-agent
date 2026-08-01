"""End-to-end contracts for shared-auth migration and recovery artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import pytest


@pytest.fixture()
def migration_home(tmp_path: Path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return root, profile


def _write(path: Path, value: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


def test_dry_run_is_redacted_and_uses_private_artifact(migration_home):
    from hermes_cli.auth_migration import plan_shared_migration

    _, profile = migration_home
    token = "super-secret-access-token"
    _write(profile / "auth.json", {"providers": {"nous": {"access_token": token}}})

    plan = plan_shared_migration(profile="coder")

    public = json.dumps(plan.manifest, sort_keys=True)
    artifact = plan.artifact_path.read_text()
    assert token not in public
    assert token not in artifact
    assert plan.plan_digest in artifact
    assert plan.manifest["sources"][0]["providers"] == ["nous"]
    if os.name != "nt":
        assert plan.artifact_path.stat().st_mode & 0o777 == 0o600


def test_apply_merges_under_shared_authority_without_overwriting_source(migration_home):
    from hermes_cli.auth_migration import apply_shared_migration, plan_shared_migration

    root, profile = migration_home
    source_raw = _write(
        profile / "auth.json",
        {"providers": {"nous": {"access_token": "profile-token"}}},
    )
    _write(
        root / "auth.json", {"providers": {"openai-codex": {"access_token": "shared"}}}
    )

    plan = plan_shared_migration(profile="coder")
    applied = apply_shared_migration(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        conflict_policy="abort",
    )

    assert applied == plan.plan_id
    merged = json.loads((root / "auth.json").read_text())
    assert set(merged["providers"]) == {"nous", "openai-codex"}
    assert (profile / "auth.json").read_bytes() == source_raw
    assert "authority: shared" in (profile / "config.yaml").read_text()
    journal = json.loads(
        (
            root
            / "state-snapshots"
            / "auth-migrations"
            / "journals"
            / f"{plan.plan_id}.json"
        ).read_text()
    )
    assert journal["phase"] == "committed"
    if os.name != "nt":
        assert (root / "auth.json").stat().st_mode & 0o777 == 0o600


def test_apply_rejects_stale_plan_before_writing(migration_home):
    from hermes_cli.auth_migration import (
        AuthMigrationError,
        apply_shared_migration,
        plan_shared_migration,
    )

    root, profile = migration_home
    _write(profile / "auth.json", {"providers": {"nous": {"access_token": "before"}}})
    plan = plan_shared_migration(profile="coder")
    _write(profile / "auth.json", {"providers": {"nous": {"access_token": "after"}}})

    with pytest.raises(AuthMigrationError, match="changed after dry-run"):
        apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
        )
    assert not (root / "auth.json").exists()


def test_explicit_rollback_restores_committed_pre_state(migration_home):
    from hermes_cli.auth_migration import (
        apply_shared_migration,
        plan_shared_migration,
        rollback_shared_migration,
    )

    root, profile = migration_home
    shared_raw = _write(
        root / "auth.json", {"providers": {"openai-codex": {"access_token": "shared"}}}
    )
    profile_raw = _write(
        profile / "auth.json", {"providers": {"nous": {"access_token": "profile"}}}
    )
    config_raw = b"display:\n  skin: mono\n"
    (profile / "config.yaml").write_bytes(config_raw)
    plan = plan_shared_migration(profile="coder")
    apply_shared_migration(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        conflict_policy="abort",
    )

    assert rollback_shared_migration(plan_id=plan.plan_id) == "rolled_back"
    assert (root / "auth.json").read_bytes() == shared_raw
    assert (profile / "auth.json").read_bytes() == profile_raw
    assert (profile / "config.yaml").read_bytes() == config_raw
    assert rollback_shared_migration(plan_id=plan.plan_id) == "rolled_back"


def test_explicit_rollback_refuses_changed_committed_state(migration_home):
    from hermes_cli.auth_migration import (
        AuthMigrationError,
        apply_shared_migration,
        plan_shared_migration,
        rollback_shared_migration,
    )

    root, profile = migration_home
    _write(profile / "auth.json", {"providers": {"nous": {"access_token": "profile"}}})
    plan = plan_shared_migration(profile="coder")
    apply_shared_migration(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        conflict_policy="abort",
    )
    changed = _write(
        root / "auth.json", {"providers": {"nous": {"access_token": "rotated"}}}
    )

    with pytest.raises(AuthMigrationError, match="changed after migration"):
        rollback_shared_migration(plan_id=plan.plan_id)
    assert (root / "auth.json").read_bytes() == changed


def test_conflict_policy_is_explicit_and_abort_is_non_destructive(migration_home):
    from hermes_cli.auth_migration import (
        AuthMigrationError,
        apply_shared_migration,
        plan_shared_migration,
    )

    root, profile = migration_home
    shared_raw = _write(
        root / "auth.json", {"providers": {"nous": {"access_token": "shared"}}}
    )
    profile_raw = _write(
        profile / "auth.json", {"providers": {"nous": {"access_token": "profile"}}}
    )
    plan = plan_shared_migration(profile="coder")

    with pytest.raises(AuthMigrationError, match="Divergent"):
        apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
        )
    assert (root / "auth.json").read_bytes() == shared_raw
    assert (profile / "auth.json").read_bytes() == profile_raw


def test_apply_rejects_unreviewed_digest(migration_home):
    from hermes_cli.auth_migration import (
        AuthMigrationError,
        apply_shared_migration,
        plan_shared_migration,
    )

    _, profile = migration_home
    _write(profile / "auth.json", {"providers": {"nous": {"access_token": "token"}}})
    plan = plan_shared_migration(profile="coder")
    with pytest.raises(AuthMigrationError, match="digest"):
        apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest="0" * 64,
            conflict_policy="prefer-shared",
        )


def test_migration_rejects_profile_outside_profiles_root(
    migration_home, tmp_path: Path
):
    from hermes_cli.auth_migration import AuthMigrationError, plan_shared_migration

    external = tmp_path / "external-profile"
    external.mkdir()
    _write(external / "auth.json", {"providers": {}})
    (migration_home[0] / "profiles" / "escaped").symlink_to(
        external, target_is_directory=True
    )
    with pytest.raises(AuthMigrationError, match="outside the Hermes profiles root"):
        plan_shared_migration(profile="escaped")


def test_migration_aborts_when_relevant_gateway_is_running(
    migration_home, monkeypatch
):
    from gateway import status as gateway_status
    from hermes_cli.auth_migration import (
        AuthMigrationError,
        apply_shared_migration,
        plan_shared_migration,
    )

    root, profile = migration_home
    plan = plan_shared_migration(profile="coder")
    monkeypatch.setattr(gateway_status, "get_running_pid", lambda *args, **kwargs: 12345)

    with pytest.raises(AuthMigrationError, match="gateway.*12345"):
        apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
        )

    journal = json.loads(
        (
            root
            / "state-snapshots"
            / "auth-migrations"
            / "journals"
            / f"{plan.plan_id}.json"
        ).read_text()
    )
    assert journal["phase"] == "aborted"


def test_quick_snapshot_excludes_auth_by_default_and_restores_encrypted_auth(
    migration_home,
):
    from hermes_cli.backup import create_quick_snapshot, restore_quick_snapshot

    root, profile = migration_home
    _write(root / "auth.json", {"providers": {"nous": {"access_token": "before"}}})
    (profile / "config.yaml").write_text("auth:\n  authority: shared\n")
    default_id = create_quick_snapshot(label="no-auth", hermes_home=profile)
    assert default_id is not None
    default_manifest = json.loads(
        (profile / "state-snapshots" / default_id / "manifest.json").read_text()
    )
    assert default_manifest["auth_authority"] is None

    passphrase = profile / "passphrase"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    snap_id = create_quick_snapshot(
        label="with-auth",
        hermes_home=profile,
        auth_mode="include-encrypted",
        auth_passphrase_file=str(passphrase),
    )
    assert snap_id is not None
    snap_dir = profile / "state-snapshots" / snap_id
    manifest = json.loads((snap_dir / "manifest.json").read_text())
    assert manifest["auth_authority"]["authority"] == "shared"
    assert b"before" not in (snap_dir / "_auth" / "authority.enc").read_bytes()

    _write(root / "auth.json", {"providers": {"nous": {"access_token": "after"}}})
    assert restore_quick_snapshot(snap_id, hermes_home=profile)
    current = json.loads((root / "auth.json").read_text())
    assert current["providers"]["nous"]["access_token"] == "after"

    # Naming a restore destination is not enough: callers must explicitly opt
    # into restoring credentials as well.
    assert restore_quick_snapshot(
        snap_id,
        hermes_home=profile,
        auth_action="restore-shared",
        auth_passphrase_file=str(passphrase),
    )
    still_current = json.loads((root / "auth.json").read_text())
    assert still_current["providers"]["nous"]["access_token"] == "after"

    assert restore_quick_snapshot(
        snap_id,
        hermes_home=profile,
        include_auth=True,
        auth_action="restore-shared",
        auth_passphrase_file=str(passphrase),
    )
    restored = json.loads((root / "auth.json").read_text())
    assert restored["providers"]["nous"]["access_token"] == "before"


def test_quick_snapshot_include_encrypted_fails_without_passphrase(migration_home):
    from hermes_cli.backup import create_quick_snapshot

    root, profile = migration_home
    _write(root / "auth.json", {"providers": {"nous": {"access_token": "before"}}})
    (profile / "config.yaml").write_text("auth:\n  authority: shared\n")

    with pytest.raises(ValueError, match="passphrase file is required"):
        create_quick_snapshot(
            label="missing-passphrase",
            hermes_home=profile,
            auth_mode="include-encrypted",
        )


def test_recovery_rolls_back_interrupted_commit(migration_home, monkeypatch):
    import hermes_cli.auth_migration as migration

    root, profile = migration_home
    original_shared = _write(
        root / "auth.json", {"providers": {"openai-codex": {"access_token": "shared"}}}
    )
    original_profile = _write(
        profile / "auth.json", {"providers": {"nous": {"access_token": "profile"}}}
    )
    plan = migration.plan_shared_migration(profile="coder")

    def crash(_path):
        raise RuntimeError("injected crash after target write")

    monkeypatch.setattr(migration, "_set_shared_authority", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        migration.apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
        )
    merged = json.loads((root / "auth.json").read_text())
    assert set(merged["providers"]) == {"nous", "openai-codex"}

    assert migration.recover_shared_migration(plan_id=plan.plan_id) == "rolled_back"
    assert (root / "auth.json").read_bytes() == original_shared
    assert (profile / "auth.json").read_bytes() == original_profile
    assert not (profile / "config.yaml").exists()
    assert migration.recover_shared_migration(plan_id=plan.plan_id) == "rolled_back"


def test_recovery_refuses_to_overwrite_state_changed_after_interrupted_commit(
    migration_home,
):
    import hermes_cli.auth_migration as migration

    root, profile = migration_home
    _write(root / "auth.json", {"providers": {"openai-codex": {"token": "before"}}})
    _write(profile / "auth.json", {"providers": {"nous": {"token": "profile"}}})
    plan = migration.plan_shared_migration(profile="coder")

    def fail(phase: str) -> None:
        if phase == "target_written":
            raise RuntimeError("injected after target write")

    with pytest.raises(RuntimeError, match="injected"):
        migration.apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
            failure_injector=fail,
        )
    changed = _write(
        root / "auth.json", {"providers": {"nous": {"token": "rotated-after-crash"}}}
    )

    with pytest.raises(migration.AuthMigrationError, match="changed after interruption"):
        migration.recover_shared_migration(plan_id=plan.plan_id)

    assert (root / "auth.json").read_bytes() == changed
    journal = json.loads(
        (
            root
            / "state-snapshots"
            / "auth-migrations"
            / "journals"
            / f"{plan.plan_id}.json"
        ).read_text()
    )
    assert journal["phase"] == "manual_required"
    assert journal["reason"] == "committed_state_changed"


def test_recovery_after_backup_only_preserves_later_writes(migration_home):
    import hermes_cli.auth_migration as migration

    root, profile = migration_home
    _write(root / "auth.json", {"providers": {"openai-codex": {"token": "before"}}})
    _write(profile / "auth.json", {"providers": {"nous": {"token": "profile"}}})
    plan = migration.plan_shared_migration(profile="coder")

    def fail(phase: str) -> None:
        if phase == "backed_up":
            raise RuntimeError("injected after backup")

    with pytest.raises(RuntimeError, match="injected"):
        migration.apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
            failure_injector=fail,
        )
    changed = _write(
        root / "auth.json", {"providers": {"openai-codex": {"token": "later"}}}
    )

    assert migration.recover_shared_migration(plan_id=plan.plan_id) == "aborted"
    assert (root / "auth.json").read_bytes() == changed
    journal = json.loads(
        (
            root
            / "state-snapshots"
            / "auth-migrations"
            / "journals"
            / f"{plan.plan_id}.json"
        ).read_text()
    )
    assert journal["phase"] == "aborted"
    assert journal["reason"] == "external_change_after_backup"


def test_concurrent_source_write_is_not_lost_and_invalidates_plan(migration_home):
    from hermes_cli.auth import _auth_store_lock
    from hermes_cli.auth_migration import AuthMigrationError, apply_shared_migration
    from hermes_cli.auth_migration import plan_shared_migration

    root, profile = migration_home
    source = profile / "auth.json"
    _write(source, {"providers": {"nous": {"access_token": "planned"}}})
    plan = plan_shared_migration(profile="coder")
    writer_locked = threading.Event()
    allow_write = threading.Event()
    apply_done = threading.Event()
    outcome: list[BaseException] = []

    def writer():
        with _auth_store_lock(target_path=source):
            writer_locked.set()
            assert allow_write.wait(5)
            _write(source, {"providers": {"nous": {"access_token": "concurrent"}}})

    def apply():
        try:
            apply_shared_migration(
                plan_id=plan.plan_id,
                plan_digest=plan.plan_digest,
                conflict_policy="abort",
            )
        except BaseException as exc:
            outcome.append(exc)
        finally:
            apply_done.set()

    writer_thread = threading.Thread(target=writer)
    apply_thread = threading.Thread(target=apply)
    writer_thread.start()
    assert writer_locked.wait(5)
    apply_thread.start()
    time.sleep(0.1)
    assert not apply_done.is_set()
    allow_write.set()
    writer_thread.join(5)
    apply_thread.join(5)

    assert len(outcome) == 1
    assert isinstance(outcome[0], AuthMigrationError)
    assert "changed after dry-run" in str(outcome[0])
    saved = json.loads(source.read_text())
    assert saved["providers"]["nous"]["access_token"] == "concurrent"
    assert not (root / "auth.json").exists()
