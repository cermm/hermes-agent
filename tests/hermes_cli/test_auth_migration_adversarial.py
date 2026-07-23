"""Adversarial concurrency and crash-window contracts for shared auth migration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def migration_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def test_gateway_quiescence_includes_unselected_shared_profiles(
    migration_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway import status as gateway_status
    from hermes_cli.auth_migration import (
        AuthMigrationError,
        apply_shared_migration,
        plan_shared_migration,
    )

    root, profile = migration_home
    _write(profile / "auth.json", {"providers": {"nous": {"token": "profile"}}})
    peer = root / "profiles" / "already-shared"
    peer.mkdir(parents=True)
    (peer / "config.yaml").write_text(
        "auth:\n  authority: shared\n", encoding="utf-8"
    )
    plan = plan_shared_migration(profile="coder")
    assert str(peer) in json.loads(plan.artifact_path.read_text())["gateway_homes"]

    def running_peer(pid_path: Path, **_kwargs):
        return 9876 if pid_path == peer / "gateway.pid" else None

    monkeypatch.setattr(gateway_status, "get_running_pid", running_peer)
    with pytest.raises(AuthMigrationError, match="gateway.*9876"):
        apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
        )


def test_gateway_quiescence_uses_runtime_status_when_pid_artifacts_are_missing(
    migration_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway import status as gateway_status
    from hermes_cli.auth_migration import (
        AuthMigrationError,
        apply_shared_migration,
        plan_shared_migration,
    )

    root, profile = migration_home
    _write(profile / "auth.json", {"providers": {"nous": {"token": "profile"}}})
    peer = root / "profiles" / "runtime-only"
    peer.mkdir(parents=True)
    (peer / "config.yaml").write_text(
        "auth:\n  authority: shared\n", encoding="utf-8"
    )
    (peer / "gateway_state.json").write_text(
        json.dumps({"pid": os.getpid(), "gateway_state": "running"}),
        encoding="utf-8",
    )
    plan = plan_shared_migration(profile="coder")
    monkeypatch.setattr(gateway_status, "get_running_pid", lambda *_args, **_kwargs: None)

    with pytest.raises(AuthMigrationError, match=f"gateway.*{os.getpid()}"):
        apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
        )


@pytest.mark.parametrize("failure_phase", ["target_write_pending", "profile_written"])
def test_recovery_covers_mutation_journal_windows(
    migration_home, failure_phase: str
) -> None:
    import hermes_cli.auth_migration as migration

    root, profile = migration_home
    original_shared = _write(
        root / "auth.json", {"providers": {"openai-codex": {"token": "shared"}}}
    )
    _write(profile / "auth.json", {"providers": {"nous": {"token": "profile"}}})
    plan = migration.plan_shared_migration(profile="coder")

    def fail(phase: str) -> None:
        if phase == failure_phase:
            raise RuntimeError(f"injected at {phase}")

    with pytest.raises(RuntimeError, match="injected"):
        migration.apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
            failure_injector=fail,
        )
    assert migration.recover_shared_migration(plan_id=plan.plan_id) in {
        "aborted",
        "rolled_back",
    }
    assert (root / "auth.json").read_bytes() == original_shared
    assert not (profile / "config.yaml").exists()


def test_manual_recovery_is_retryable_after_expected_state_is_restored(
    migration_home,
) -> None:
    import hermes_cli.auth_migration as migration

    root, profile = migration_home
    original_shared = _write(
        root / "auth.json", {"providers": {"openai-codex": {"token": "shared"}}}
    )
    _write(profile / "auth.json", {"providers": {"nous": {"token": "profile"}}})
    plan = migration.plan_shared_migration(profile="coder")

    def fail(phase: str) -> None:
        if phase == "target_written":
            raise RuntimeError("injected after target")

    with pytest.raises(RuntimeError, match="injected"):
        migration.apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
            failure_injector=fail,
        )
    expected_migration = (root / "auth.json").read_bytes()
    _write(root / "auth.json", {"providers": {"nous": {"token": "external"}}})
    with pytest.raises(migration.AuthMigrationError, match="changed after interruption"):
        migration.recover_shared_migration(plan_id=plan.plan_id)

    (root / "auth.json").write_bytes(expected_migration)
    assert migration.recover_shared_migration(plan_id=plan.plan_id) == "rolled_back"
    assert (root / "auth.json").read_bytes() == original_shared


def test_recovery_detects_changed_committed_state(migration_home) -> None:
    import hermes_cli.auth_migration as migration

    root, profile = migration_home
    _write(profile / "auth.json", {"providers": {"nous": {"token": "profile"}}})
    plan = migration.plan_shared_migration(profile="coder")
    migration.apply_shared_migration(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        conflict_policy="abort",
    )
    changed = _write(root / "auth.json", {"providers": {"nous": {"token": "later"}}})

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


def test_manifest_records_each_artifact_with_stable_profile_identity(
    migration_home,
) -> None:
    import hermes_cli.auth_migration as migration

    root, profile = migration_home
    (profile / "config.yaml").write_text(
        "auth:\n  authority: profile\n",
        encoding="utf-8",
    )
    _write(profile / "auth.json", {"providers": {}})

    plan = migration.plan_shared_migration(profile="coder")

    source = plan.manifest["sources"][0]
    assert source["profile_id"] == "coder"
    assert source["artifacts"] == [
        {
            "artifact_class": "profile-auth",
            "exists": True,
            "profile_id": "coder",
        },
        {
            "artifact_class": "profile-config",
            "exists": True,
            "profile_id": "coder",
        },
    ]
    assert plan.manifest["target_artifact"] == {
        "artifact_class": "shared-auth",
        "exists": False,
    }
