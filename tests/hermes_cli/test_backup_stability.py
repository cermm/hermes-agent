from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from hermes_cli.backup import (
    BackupInProgressError,
    _atomic_output_path,
    _backup_operation_lock,
    _write_full_zip_backup,
    create_quick_snapshot,
    list_quick_snapshots,
)


def test_backup_lock_rejects_a_second_operation(tmp_path) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()

    with _backup_operation_lock(home):
        with pytest.raises(BackupInProgressError):
            with _backup_operation_lock(home, timeout_seconds=0):
                raise AssertionError("second backup unexpectedly acquired the lock")


def test_atomic_output_publishes_only_after_clean_close(tmp_path) -> None:
    final = tmp_path / "backup.zip"
    final.write_bytes(b"previous")

    with _atomic_output_path(final) as (archive, partial):
        archive.write(b"complete")
        archive.flush()
        assert final.read_bytes() == b"previous"

    assert final.read_bytes() == b"complete"
    assert not partial.exists()


def test_atomic_output_keeps_previous_file_after_failure(tmp_path) -> None:
    final = tmp_path / "backup.zip"
    final.write_bytes(b"previous")
    partial = None

    with pytest.raises(RuntimeError):
        with _atomic_output_path(final) as (archive, partial):
            archive.write(b"incomplete")
            raise RuntimeError("compression failed")

    assert final.read_bytes() == b"previous"
    assert partial is not None
    assert not partial.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
def test_atomic_output_descriptor_is_private_under_permissive_umask(tmp_path) -> None:
    final = tmp_path / "backup.zip"
    old_umask = os.umask(0o000)
    try:
        with _atomic_output_path(final) as (archive, _partial):
            archive.write(b"credential-bearing backup")
            archive.flush()
            assert stat.S_IMODE(os.fstat(archive.fileno()).st_mode) == 0o600
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(final.stat().st_mode) == 0o600


def test_atomic_output_rejects_preexisting_partial_symlink(
    tmp_path, monkeypatch
) -> None:
    from hermes_cli import backup

    final = tmp_path / "backup.zip"
    target = tmp_path / "attacker-controlled"
    target.write_bytes(b"do not overwrite")
    real_open = backup.os.open
    planted_partial = None

    def plant_symlink_then_open(path, flags, mode=0o777):
        nonlocal planted_partial
        partial = Path(path)
        partial.symlink_to(target)
        planted_partial = partial
        return real_open(path, flags, mode)

    monkeypatch.setattr(backup.os, "open", plant_symlink_then_open)

    with pytest.raises(FileExistsError):
        with _atomic_output_path(final) as (archive, _partial):
            archive.write(b"credential-bearing backup")

    assert target.read_bytes() == b"do not overwrite"
    assert planted_partial is not None
    assert planted_partial.is_symlink()
    assert not final.exists()


def test_atomic_output_refuses_to_publish_replaced_partial_inode(tmp_path) -> None:
    final = tmp_path / "backup.zip"
    final.write_bytes(b"previous")
    attacker = tmp_path / "attacker-controlled"
    attacker.write_bytes(b"attacker payload")
    partial = None

    with pytest.raises(RuntimeError, match="replaced"):
        with _atomic_output_path(final) as (archive, partial):
            archive.write(b"complete")
            archive.flush()
            partial.unlink()
            partial.symlink_to(attacker)

    assert final.read_bytes() == b"previous"
    assert attacker.read_bytes() == b"attacker payload"
    assert partial is not None
    assert partial.is_symlink()


def test_quick_snapshot_is_published_with_manifest(tmp_path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    published: list[tuple[Path, Path]] = []

    from hermes_cli import backup

    real_replace = backup.os.replace

    def replace(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path.parent == home / "state-snapshots":
            assert source_path.name.endswith(".partial")
            assert (source_path / "manifest.json").is_file()
            assert not destination_path.exists()
            published.append((source_path, destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(backup.os, "replace", replace)
    snapshot_id = create_quick_snapshot(hermes_home=home)

    assert snapshot_id is not None
    assert len(published) == 1
    manifest = json.loads(
        (home / "state-snapshots" / snapshot_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["id"] == snapshot_id
    assert manifest["files"] == {"config.yaml": 10}


def test_quick_snapshot_listing_ignores_partial_directories(tmp_path) -> None:
    home = tmp_path / ".hermes"
    partial = home / "state-snapshots" / ".unfinished.1.partial"
    partial.mkdir(parents=True)
    (partial / "manifest.json").write_text('{"id":"unfinished"}', encoding="utf-8")

    assert list_quick_snapshots(hermes_home=home) == []


def test_failed_automatic_backup_preserves_previous_archive(tmp_path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "state.db").write_bytes(b"not-a-database")
    archive = tmp_path / "automatic.zip"
    archive.write_bytes(b"previous-valid-backup")

    monkeypatch.setattr("hermes_cli.backup._safe_copy_db", lambda _src, _dst: False)

    assert _write_full_zip_backup(archive, home) is None
    assert archive.read_bytes() == b"previous-valid-backup"
    assert list(tmp_path.glob(".*.partial")) == []
