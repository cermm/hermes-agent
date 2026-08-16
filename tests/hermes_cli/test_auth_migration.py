"""Focused dry-run/recovery shared-auth migration regressions for issue #380."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path


def _reset_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))


def test_migrate_shared_dry_run_is_redacted_and_write_free(tmp_path, monkeypatch, capsys):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "builder-high"
    profile.mkdir(parents=True)
    profile_auth = profile / "auth.json"
    before = {
        "version": 1,
        "providers": {"openai-codex": {"access_token": "not-a-real-token", "refresh_token": "not-a-real-refresh"}},
        "credential_pool": {"openai-codex": [{"id": "codex1", "access_token": "pool-token", "refresh_token": "pool-refresh"}]},
    }
    profile_auth.write_text(json.dumps(before, sort_keys=True), encoding="utf-8")
    _reset_home(monkeypatch, root)

    from hermes_cli.auth_commands import auth_migrate_shared_command

    args = type("Args", (), {
        "all_profiles": True,
        "profile": None,
        "dry_run": True,
        "apply": False,
        "recover": False,
        "rollback": False,
        "plan_id": None,
        "plan_digest": None,
        "conflict_policy": "abort",
    })()
    auth_migrate_shared_command(args)
    out = capsys.readouterr().out

    assert "Auth migration dry-run" in out
    assert "builder-high" in out
    assert "openai-codex" in out
    assert "not-a-real-token" not in out
    assert "not-a-real-refresh" not in out
    assert json.loads(profile_auth.read_text(encoding="utf-8")) == before
    assert not (root / "auth.json").exists()


def test_migrate_recover_missing_plan_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    _reset_home(monkeypatch, root)

    import pytest
    from hermes_cli.auth_migration import AuthMigrationError, recover_shared_migration

    with pytest.raises(AuthMigrationError, match="journal was not found"):
        recover_shared_migration(plan_id="missing")


def test_migrate_shared_apply_commits_target_and_profile_configs(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "builder-high"
    profile.mkdir(parents=True)
    (profile / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {"openai-codex": {"access_token": "apply-access"}},
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "codex1",
                            "auth_type": "oauth",
                            "source": "manual:device_code",
                            "access_token": "apply-access",
                        }
                    ]
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _reset_home(monkeypatch, root)

    from hermes_cli.auth_migration import apply_shared_migration, plan_shared_migration

    plan = plan_shared_migration(all_profiles=True)
    assert apply_shared_migration(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        conflict_policy="abort",
    ) == plan.plan_id

    shared = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    assert "openai-codex" in shared["providers"]
    assert shared["credential_pool"]["openai-codex"][0]["id"] == "codex1"
    assert "auth:\n  authority: shared\n" in (profile / "config.yaml").read_text(encoding="utf-8")


def test_migrate_shared_dry_run_manifest_and_preconditions_share_locked_snapshot(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "builder-high"
    profile.mkdir(parents=True)
    (profile / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {"openai-codex": {"access_token": "snapshot-access"}}}),
        encoding="utf-8",
    )
    _reset_home(monkeypatch, root)

    import hermes_cli.auth_migration as migration

    state = {"transition": 0, "stores": 0}
    events: list[tuple[str, bool, bool]] = []
    original_manifest = migration._redacted_manifest
    original_precondition = migration._content_precondition

    @contextmanager
    def transition_lock():
        state["transition"] += 1
        try:
            yield
        finally:
            state["transition"] -= 1

    @contextmanager
    def store_locks(*_args, **_kwargs):
        state["stores"] += 1
        try:
            yield
        finally:
            state["stores"] -= 1

    def recording_manifest(*args, **kwargs):
        events.append(("manifest", state["transition"] > 0, state["stores"] > 0))
        return original_manifest(*args, **kwargs)

    def recording_precondition(path):
        events.append(("precondition", state["transition"] > 0, state["stores"] > 0))
        return original_precondition(path)

    monkeypatch.setattr(migration, "_auth_transition_lock", transition_lock)
    monkeypatch.setattr(migration, "_auth_store_locks", store_locks)
    monkeypatch.setattr(migration, "_redacted_manifest", recording_manifest)
    monkeypatch.setattr(migration, "_content_precondition", recording_precondition)

    plan = migration.plan_shared_migration(all_profiles=True)

    assert plan.plan_digest == migration._manifest_digest(plan.manifest)
    assert any(kind == "manifest" for kind, _, _ in events)
    assert any(kind == "precondition" for kind, _, _ in events)
    assert all(transition and stores for _, transition, stores in events)
