from __future__ import annotations

from hermes_cli.cli_commands_mixin import CLICommandsMixin


def test_snapshot_restore_skips_auth_by_default(monkeypatch, capsys) -> None:
    captured = {}

    def restore(snapshot_id, **kwargs):
        captured.update(snapshot_id=snapshot_id, **kwargs)
        return True

    monkeypatch.setattr("hermes_cli.backup.restore_quick_snapshot", restore)
    monkeypatch.setattr("hermes_cli.backup.list_quick_snapshots", lambda **kwargs: [])

    CLICommandsMixin()._handle_snapshot_command("/snapshot restore snap-1")

    assert captured == {
        "snapshot_id": "snap-1",
        "include_auth": False,
        "auth_action": "skip",
        "auth_passphrase_file": None,
    }
    assert "Authentication was skipped" in capsys.readouterr().out


def test_snapshot_restore_auth_requires_explicit_destination_and_passphrase(
    monkeypatch, capsys
) -> None:
    called = False

    def restore(*args, **kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr("hermes_cli.backup.restore_quick_snapshot", restore)
    monkeypatch.setattr("hermes_cli.backup.list_quick_snapshots", lambda **kwargs: [])

    CLICommandsMixin()._handle_snapshot_command(
        "/snapshot restore snap-1 --include-auth"
    )

    assert called is False
    assert "requires --auth-action" in capsys.readouterr().out


def test_snapshot_restore_passes_explicit_auth_gate(monkeypatch) -> None:
    captured = {}

    def restore(snapshot_id, **kwargs):
        captured.update(snapshot_id=snapshot_id, **kwargs)
        return True

    monkeypatch.setattr("hermes_cli.backup.restore_quick_snapshot", restore)
    monkeypatch.setattr("hermes_cli.backup.list_quick_snapshots", lambda **kwargs: [])

    CLICommandsMixin()._handle_snapshot_command(
        "/snapshot restore snap-1 --include-auth --auth-action restore-profile "
        "--auth-passphrase-file /secure/passphrase",
    )

    assert captured == {
        "snapshot_id": "snap-1",
        "include_auth": True,
        "auth_action": "restore-profile",
        "auth_passphrase_file": "/secure/passphrase",
    }
