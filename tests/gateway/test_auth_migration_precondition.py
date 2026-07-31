from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def gateway_auth_home(tmp_path: Path, monkeypatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "gateway"
    profile.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return root


def _journal(root: Path, phase: str) -> None:
    path = (
        root
        / "state-snapshots"
        / "auth-migrations"
        / "journals"
        / "plan-gateway.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"plan_id": "plan-gateway", "phase": phase}))


@pytest.mark.parametrize(
    "phase",
    ["planned", "locked", "backed_up", "target_written", "profiles_configured", "manual_required"],
)
def test_gateway_startup_precondition_rejects_incomplete_migration(
    gateway_auth_home: Path, phase: str
) -> None:
    from gateway.run import _auth_migration_startup_ready

    _journal(gateway_auth_home, phase)

    assert _auth_migration_startup_ready() is False


@pytest.mark.parametrize("phase", ["committed", "rolled_back", "aborted"])
def test_gateway_startup_precondition_accepts_terminal_migration(
    gateway_auth_home: Path, phase: str
) -> None:
    from gateway.run import _auth_migration_startup_ready

    _journal(gateway_auth_home, phase)

    assert _auth_migration_startup_ready() is True


@pytest.mark.asyncio
async def test_start_gateway_stops_before_runtime_lock_for_incomplete_migration(
    gateway_auth_home: Path, monkeypatch
) -> None:
    import gateway.run as gateway_run

    _journal(gateway_auth_home, "target_written")
    monkeypatch.setattr(
        "gateway.code_skew.record_boot_fingerprint", lambda: None
    )
    monkeypatch.setattr(
        "gateway.status.get_running_pid",
        lambda: (_ for _ in ()).throw(
            AssertionError("runtime lock discovery must not run")
        ),
    )

    assert await gateway_run.start_gateway() is False
