from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture
def profile_root(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def test_clone_profile_requires_explicit_auth_mode_for_local_credentials(profile_root):
    from hermes_cli.profiles import create_profile

    source = {"providers": {"nous": {"access_token": "shared-token"}}}
    (profile_root / "auth.json").write_text(json.dumps(source), encoding="utf-8")

    local = create_profile(
        "local",
        clone_from="default",
        auth_mode="profile",
        no_alias=True,
    )
    assert json.loads((local / "auth.json").read_text()) == source
    assert yaml.safe_load((local / "config.yaml").read_text())["auth"]["authority"] == "profile"

    shared = create_profile(
        "shared",
        clone_from="default",
        auth_mode="shared",
        no_alias=True,
    )
    assert not (shared / "auth.json").exists()
    assert yaml.safe_load((shared / "config.yaml").read_text())["auth"]["authority"] == "shared"


def test_new_profile_defaults_to_explicit_shared_authority(profile_root):
    from hermes_cli.profiles import create_profile

    created = create_profile("fresh", no_alias=True)

    assert yaml.safe_load((created / "config.yaml").read_text())["auth"]["authority"] == "shared"
    assert not (created / "auth.json").exists()


def test_delete_profile_local_auth_requires_purge_or_archive(profile_root):
    from hermes_cli.profiles import create_profile, delete_profile

    local = create_profile("local", auth_mode="profile", no_alias=True)
    (local / "auth.json").write_text('{"providers":{}}', encoding="utf-8")

    with pytest.raises(ValueError, match="--auth-action"):
        delete_profile("local", yes=True)

    with patch("hermes_cli.profiles._cleanup_gateway_service"):
        delete_profile("local", yes=True, auth_action="archive")
    archives = list(
        (profile_root / "state-snapshots" / "auth-profile-deletions").glob(
            "local-*.json"
        )
    )
    assert len(archives) == 1
    assert archives[0].stat().st_mode & 0o777 == 0o600


def test_delete_shared_profile_does_not_treat_shared_store_as_local(profile_root):
    from hermes_cli.profiles import create_profile, delete_profile

    (profile_root / "auth.json").write_text('{"providers":{}}', encoding="utf-8")
    shared = create_profile("shared", auth_mode="shared", no_alias=True)

    with patch("hermes_cli.profiles._cleanup_gateway_service"):
        deleted = delete_profile("shared", yes=True)

    assert deleted == shared
    assert not shared.exists()
    assert (profile_root / "auth.json").is_file()


def test_rename_moves_profile_local_authority_without_touching_shared(profile_root):
    from hermes_cli.profiles import create_profile, rename_profile

    shared_raw = '{"providers":{"nous":{"access_token":"shared"}}}'
    (profile_root / "auth.json").write_text(shared_raw, encoding="utf-8")
    local = create_profile("before", auth_mode="profile", no_alias=True)
    (local / "auth.json").write_text('{"providers":{"nous":{"access_token":"local"}}}', encoding="utf-8")

    with patch("hermes_cli.profiles._cleanup_gateway_service"):
        renamed = rename_profile("before", "after")
    assert (renamed / "auth.json").is_file()
    assert yaml.safe_load((renamed / "config.yaml").read_text())["auth"]["authority"] == "profile"
    assert (profile_root / "auth.json").read_text() == shared_raw


def test_rename_stops_profile_backends_before_moving_local_authority(profile_root):
    from hermes_cli.profiles import create_profile, rename_profile

    local = create_profile("before", auth_mode="profile", no_alias=True)
    (local / "auth.json").write_text('{"providers":{}}', encoding="utf-8")
    events = []
    real_rename = Path.rename

    with patch(
        "hermes_cli.profiles._stop_profile_backends",
        side_effect=lambda *_args: events.append("backends-stopped"),
    ), patch(
        "hermes_cli.profiles._cleanup_gateway_service",
        side_effect=lambda *_args, **_kwargs: events.append("gateway-stopped"),
    ), patch.object(
        Path,
        "rename",
        autospec=True,
        side_effect=lambda source, destination: (
            events.append("renamed"),
            real_rename(source, destination),
        )[1],
    ):
        rename_profile("before", "after")

    assert events.index("backends-stopped") < events.index("renamed")
    assert events.index("gateway-stopped") < events.index("renamed")


def test_delete_archives_profile_auth_only_after_writers_stop(
    tmp_path, monkeypatch
) -> None:
    import hermes_cli.profiles as profiles

    root = tmp_path / ".hermes"
    target = root / "profiles" / "local"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(
        "auth:\n  authority: profile\n", encoding="utf-8"
    )
    (target / "auth.json").write_text('{"providers": {}}', encoding="utf-8")
    events: list[str] = []

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(
        profiles,
        "_stop_profile_backends",
        lambda *_args, **_kwargs: events.append("stop"),
    )
    monkeypatch.setattr(
        profiles, "_cleanup_gateway_service", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(profiles, "_stop_gateway_process", lambda *_args: None)
    monkeypatch.setattr(
        profiles,
        "_archive_profile_auth",
        lambda *_args, **_kwargs: events.append("archive") or (root / "archived"),
    )

    assert profiles.delete_profile("local", yes=True, auth_action="archive") == target
    assert events.index("stop") < events.index("archive")
