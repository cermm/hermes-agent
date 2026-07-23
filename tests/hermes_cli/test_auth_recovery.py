from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_auth_migrate_recover_command_dispatches(monkeypatch) -> None:
    from hermes_cli import auth_commands
    import hermes_cli.auth_migration as migration

    calls: list[str] = []
    monkeypatch.setattr(
        migration,
        "recover_shared_migration",
        lambda *, plan_id: calls.append(plan_id) or "rolled_back",
    )

    auth_commands.auth_migrate_shared_command(
        SimpleNamespace(recover=True, plan_id="abc123")
    )

    assert calls == ["abc123"]


@pytest.mark.parametrize(
    "failure_phase",
    ["planned", "locked", "backed_up", "target_written", "profiles_configured"],
)
def test_failure_after_every_journal_phase_blocks_until_recovery(
    tmp_path: Path, monkeypatch, failure_phase: str
) -> None:
    from hermes_cli.auth_authority import AuthAuthorityConfigError, get_auth_store_path
    from hermes_cli.auth_migration import (
        apply_shared_migration,
        plan_shared_migration,
        recover_shared_migration,
    )

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    shared_raw = b'{"providers":{"openai-codex":{"access_token":"shared"}}}\n'
    profile_raw = b'{"providers":{"nous":{"access_token":"profile"}}}\n'
    (root / "auth.json").write_bytes(shared_raw)
    (profile / "auth.json").write_bytes(profile_raw)
    plan = plan_shared_migration(profile="coder")

    def fail(phase: str) -> None:
        if phase == failure_phase:
            raise RuntimeError(f"injected after {phase}")

    with pytest.raises(RuntimeError, match="injected"):
        apply_shared_migration(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            conflict_policy="abort",
            failure_injector=fail,
        )

    with pytest.raises(AuthAuthorityConfigError, match=plan.plan_id):
        get_auth_store_path()

    assert recover_shared_migration(plan_id=plan.plan_id) == "rolled_back"
    assert (root / "auth.json").read_bytes() == shared_raw
    assert (profile / "auth.json").read_bytes() == profile_raw
    assert not (profile / "config.yaml").exists()
    assert get_auth_store_path() == profile / "auth.json"
