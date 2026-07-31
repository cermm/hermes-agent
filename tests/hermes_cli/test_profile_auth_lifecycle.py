from __future__ import annotations

import json
import os
import stat
import threading
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


def test_clone_profile_creates_empty_local_authority_without_oauth_fork(profile_root):
    from hermes_cli.profiles import create_profile

    source = {
        "providers": {
            "openai-codex": {
                "access_token": "shared-access",
                "refresh_token": "single-use-refresh",
            }
        }
    }
    (profile_root / "auth.json").write_text(json.dumps(source), encoding="utf-8")

    local = create_profile(
        "local",
        clone_from="default",
        auth_mode="profile",
        no_alias=True,
    )
    local_auth = json.loads((local / "auth.json").read_text())
    assert local_auth["providers"] == {}
    assert "credential_pool" not in local_auth
    assert "single-use-refresh" not in (local / "auth.json").read_text()
    assert (local / "auth.json").stat().st_mode & 0o777 == 0o600
    assert yaml.safe_load((local / "config.yaml").read_text())["auth"]["authority"] == "profile"

    shared = create_profile(
        "shared",
        clone_from="default",
        auth_mode="shared",
        no_alias=True,
    )
    assert not (shared / "auth.json").exists()
    assert yaml.safe_load((shared / "config.yaml").read_text())["auth"]["authority"] == "shared"


@pytest.mark.parametrize("auth_mode", ["shared", "profile"])
def test_clone_all_sets_requested_auth_authority(profile_root, auth_mode):
    from hermes_cli.profiles import create_profile

    (profile_root / "config.yaml").write_text(
        "display:\n  skin: mono\n", encoding="utf-8"
    )

    cloned = create_profile(
        f"clone-{auth_mode}",
        clone_from="default",
        clone_all=True,
        auth_mode=auth_mode,
        no_alias=True,
    )

    config = yaml.safe_load((cloned / "config.yaml").read_text(encoding="utf-8"))
    assert config["display"]["skin"] == "mono"
    assert config["auth"]["authority"] == auth_mode
    assert (cloned / "auth.json").is_file() is (auth_mode == "profile")


@pytest.mark.parametrize("auth_mode", ["shared", "profile"])
def test_clone_all_rejects_symlinked_config_without_external_mutation(
    profile_root, tmp_path, auth_mode
):
    from hermes_cli.profiles import create_profile

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "external-config.yaml"
    sentinel_content = b"external: sentinel\n"
    sentinel.write_bytes(sentinel_content)
    sentinel.chmod(0o640)
    (profile_root / "config.yaml").symlink_to(sentinel)
    target = profile_root / "profiles" / f"escaped-{auth_mode}"

    error = None
    try:
        create_profile(
            f"escaped-{auth_mode}",
            clone_from="default",
            clone_all=True,
            auth_mode=auth_mode,
            no_alias=True,
        )
    except Exception as exc:  # assertions below verify the fail-closed contract
        error = exc

    assert sentinel.read_bytes() == sentinel_content
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert list(outside.iterdir()) == [sentinel]
    assert (profile_root / "config.yaml").is_symlink()
    assert not os.path.lexists(target)
    assert isinstance(error, ValueError)
    assert "regular file" in str(error)


def test_auth_injection_rejects_replaced_profile_directory(
    profile_root, tmp_path, monkeypatch
):
    import hermes_cli.profiles as profiles

    target = profile_root / "profiles" / "raced"
    saved_created = profile_root / "profiles" / ".raced-created"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    sentinel = replacement / "config.yaml"
    sentinel_content = b"external: sentinel\n"
    sentinel.write_bytes(sentinel_content)
    sentinel.chmod(0o640)

    real_open = profiles.os.open
    swapped = False
    raced_path = None

    def swap_before_directory_open(path, flags, *args, dir_fd=None, **kwargs):
        nonlocal raced_path, swapped
        path_name = Path(path).name
        opens_target = (
            path_name.startswith(".raced.create-")
            and flags & getattr(profiles.os, "O_DIRECTORY", 0)
        )
        if not swapped and opens_target:
            swapped = True
            raced_path = profile_root / "profiles" / path_name
            profiles.os.rename(raced_path, saved_created)
            profiles.os.rename(replacement, raced_path)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    supported_dir_fd = set(profiles.os.supports_dir_fd)
    supported_dir_fd.discard(real_open)
    supported_dir_fd.add(swap_before_directory_open)
    monkeypatch.setattr(profiles.os, "open", swap_before_directory_open)
    monkeypatch.setattr(profiles.os, "supports_dir_fd", supported_dir_fd)

    with pytest.raises((ValueError, RuntimeError), match="identity|changed|refusing"):
        profiles.create_profile("raced", auth_mode="shared", no_alias=True)

    assert swapped
    assert raced_path is not None
    assert (raced_path / "config.yaml").read_bytes() == sentinel_content
    assert stat.S_IMODE((raced_path / "config.yaml").stat().st_mode) == 0o640
    assert not (raced_path / ".env").exists()
    assert not os.path.lexists(target)
    assert saved_created.is_dir()


def test_profile_identity_remains_bound_after_authority_injection(
    profile_root, tmp_path, monkeypatch
):
    import hermes_cli.profiles as profiles

    target = profile_root / "profiles" / "post-authority-race"
    saved_created = profile_root / "profiles" / ".post-authority-created"
    replacement = tmp_path / "post-authority-replacement"
    replacement.mkdir()
    sentinel = replacement / "sentinel.txt"
    sentinel.write_text("external sentinel", encoding="utf-8")

    real_write_authority = profiles._write_profile_auth_authority
    swapped = False

    def swap_after_authority(*args, **kwargs):
        nonlocal swapped
        result = real_write_authority(*args, **kwargs)
        if target.exists():
            profiles.os.rename(target, saved_created)
        profiles.os.rename(replacement, target)
        swapped = True
        return result

    monkeypatch.setattr(
        profiles, "_write_profile_auth_authority", swap_after_authority
    )

    with pytest.raises(
        (FileExistsError, ValueError, RuntimeError),
        match="exists|identity|changed|refusing",
    ):
        profiles.create_profile(
            "post-authority-race",
            auth_mode="profile",
            description="must stay transaction-local",
            no_alias=True,
        )

    assert swapped
    assert (target / "sentinel.txt").read_text(encoding="utf-8") == "external sentinel"
    assert not (target / ".env").exists()
    assert not (target / "SOUL.md").exists()
    assert not (target / "auth.json").exists()
    assert not (target / "profile.json").exists()


def test_portable_authority_injection_fails_closed_on_directory_swap(
    profile_root, tmp_path, monkeypatch
):
    import hermes_cli.profiles as profiles

    fixed_time_ns = 4242
    target = profile_root / "profiles" / "portable-race"
    saved_created = profile_root / "profiles" / ".portable-created"
    replacement = tmp_path / "portable-replacement"
    replacement.mkdir()
    sentinel = replacement / "config.yaml"
    sentinel_content = b"external: sentinel\n"
    sentinel.write_bytes(sentinel_content)
    sentinel.chmod(0o640)
    staged_name = f".config.yaml.{os.getpid()}.{fixed_time_ns}.tmp"
    (replacement / staged_name).write_text("attacker: staged\n", encoding="utf-8")

    real_replace = profiles.os.replace
    swapped = False

    def swap_inside_replace(source, destination, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(destination).name == "config.yaml":
            swapped = True
            profiles.os.rename(target, saved_created)
            profiles.os.rename(replacement, target)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(profiles.os, "supports_dir_fd", set())
    monkeypatch.setattr(profiles.time, "time_ns", lambda: fixed_time_ns)
    monkeypatch.setattr(profiles.os, "replace", swap_inside_replace)

    error = None
    try:
        profiles.create_profile("portable-race", auth_mode="shared", no_alias=True)
    except Exception as exc:  # assertions below verify the fail-closed contract
        error = exc

    assert isinstance(error, RuntimeError)
    assert not swapped
    assert sentinel.read_bytes() == sentinel_content
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert not os.path.lexists(target)


def test_supported_portable_profile_creation_publishes_authority(
    profile_root, monkeypatch
):
    import contextlib
    import ctypes
    import hermes_cli.profiles as profiles

    @contextlib.contextmanager
    def stable_portable_directory(_profile_dir, _created_identity):
        yield

    def portable_regular_file(path, _identity):
        return Path(path).read_bytes()

    class FakeMoveFile:
        argtypes = None
        restype = None

        def __call__(self, source, destination, flags):
            assert flags == 0, "Windows publication must not replace an existing target"
            os.rename(source, destination)
            return 1

    class FakeKernel32:
        MoveFileExW = FakeMoveFile()

    monkeypatch.setattr(profiles.os, "supports_dir_fd", set())
    monkeypatch.setattr(profiles, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel32(), raising=False
    )
    monkeypatch.setattr(
        profiles,
        "_windows_profile_directory_guard",
        stable_portable_directory,
        raising=False,
    )
    monkeypatch.setattr(
        profiles,
        "_read_windows_regular_file_no_follow",
        portable_regular_file,
    )
    (profile_root / "config.yaml").write_text(
        "display:\n  skin: mono\n", encoding="utf-8"
    )

    created = profiles.create_profile(
        "portable-supported",
        clone_config=True,
        auth_mode="shared",
        no_alias=True,
    )

    raw = yaml.safe_load((created / "config.yaml").read_text(encoding="utf-8"))
    assert raw["display"]["skin"] == "mono"
    assert raw["auth"]["authority"] == "shared"


def test_windows_portable_handles_deny_delete_sharing_and_open_reparse_points(
    profile_root, monkeypatch
):
    import ctypes
    import sys
    from types import SimpleNamespace
    import hermes_cli.profiles as profiles

    calls = []

    class FakeFunction:
        argtypes = None
        restype = None

        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    def create_file(path, access, share, _security, _creation, flags, _template):
        calls.append((Path(path), access, share, flags))
        return os.open(path, os.O_RDONLY)

    class FakeKernel32:
        CreateFileW = FakeFunction(create_file)
        CloseHandle = FakeFunction(lambda handle: (os.close(handle), 1)[1])

    monkeypatch.setattr(profiles, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel32(), raising=False
    )
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda handle, _flags: handle),
    )

    staged = profile_root / "profiles" / ".portable-handles.create-test"
    staged.mkdir(parents=True)
    config = staged / "config.yaml"
    config.write_bytes(b"display:\n  skin: mono\n")
    staged_stat = staged.lstat()
    config_stat = config.lstat()

    directory_guard = getattr(profiles, "_windows_profile_directory_guard")
    secure_read = getattr(profiles, "_read_windows_regular_file_no_follow")
    with directory_guard(
        staged, (staged_stat.st_dev, staged_stat.st_ino)
    ):
        assert secure_read(
            config, (config_stat.st_dev, config_stat.st_ino)
        ).startswith(b"display:")

    directory_call, file_call = calls
    assert directory_call[2] == 0x00000001 | 0x00000002
    assert directory_call[3] & 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    assert directory_call[3] & 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    assert file_call[2] == 0x00000001 | 0x00000002
    assert file_call[3] == 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT


def test_rollback_never_recursively_deletes_replacement_directory(
    profile_root, tmp_path, monkeypatch
):
    import hermes_cli.profiles as profiles

    target = profile_root / "profiles" / "rollback-race"
    saved_created = profile_root / "profiles" / ".rollback-created"
    replacement = tmp_path / "rollback-replacement"
    replacement.mkdir()
    sentinel = replacement / "do-not-delete.txt"
    sentinel.write_text("external sentinel", encoding="utf-8")

    def fail_authority_write(*_args, **_kwargs):
        raise ValueError("forced authority failure")

    real_rename = profiles.os.rename
    real_rmtree = profiles.shutil.rmtree
    swapped = False

    swapped_path = None

    def swap_once(path: Path) -> None:
        nonlocal swapped, swapped_path
        if swapped:
            return
        swapped = True
        swapped_path = path
        real_rename(path, saved_created)
        real_rename(replacement, path)

    def swap_before_quarantine(source, destination, *args, **kwargs):
        if ".rollback-" in Path(destination).name:
            swap_once(Path(source))
        return real_rename(source, destination, *args, **kwargs)

    def swap_before_legacy_rmtree(path, *args, **kwargs):
        if Path(path).name.startswith(".rollback-race.create-"):
            swap_once(Path(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(profiles, "_write_profile_auth_authority", fail_authority_write)
    supported_dir_fd = set(profiles.os.supports_dir_fd)
    supported_dir_fd.discard(real_rename)
    supported_dir_fd.add(swap_before_quarantine)
    monkeypatch.setattr(profiles.os, "rename", swap_before_quarantine)
    monkeypatch.setattr(profiles.os, "supports_dir_fd", supported_dir_fd)
    monkeypatch.setattr(profiles.shutil, "rmtree", swap_before_legacy_rmtree)

    with pytest.raises(ValueError, match="forced authority failure"):
        profiles.create_profile("rollback-race", auth_mode="shared", no_alias=True)

    assert swapped
    assert swapped_path is not None
    assert (swapped_path / sentinel.name).read_text(encoding="utf-8") == "external sentinel"
    assert not os.path.lexists(target)
    assert saved_created.is_dir(), "unsafe cleanup must retain the transaction inode"


def test_downstream_failure_rolls_staged_profile_out_of_creation_namespace(
    profile_root, monkeypatch
):
    import hermes_cli.profiles as profiles

    gateway_registrations = []

    def fail_migration(_profile_dir):
        raise ValueError("forced downstream migration failure")

    monkeypatch.setattr(
        profiles, "_migrate_profile_config_if_outdated", fail_migration
    )
    monkeypatch.setattr(
        profiles,
        "_maybe_register_gateway_service",
        lambda name: gateway_registrations.append(name),
    )

    with pytest.raises(ValueError, match="forced downstream migration failure"):
        profiles.create_profile("full-transaction", auth_mode="profile", no_alias=True)

    profiles_root = profile_root / "profiles"
    assert not os.path.lexists(profiles_root / "full-transaction")
    assert not list(profiles_root.glob(".full-transaction.create-*"))
    assert list(profiles_root.glob("..full-transaction.create-*.rollback-*"))
    assert gateway_registrations == []


def test_posix_rollback_never_rmdirs_a_last_moment_replacement(
    profile_root, tmp_path, monkeypatch
):
    import hermes_cli.profiles as profiles

    target = profile_root / "profiles" / "posix-rmdir-race"
    saved_created = profile_root / "profiles" / ".posix-rmdir-created"
    replacement = tmp_path / "empty-external-directory"
    replacement.mkdir()
    real_rmdir = profiles.os.rmdir
    real_rename = profiles.os.rename
    swapped = False

    def fail_authority_write(*_args, **_kwargs):
        raise ValueError("forced authority failure")

    def swap_inside_final_rmdir(path, *args, **kwargs):
        nonlocal swapped
        candidate = Path(path)
        if not swapped and ".rollback-" in candidate.name:
            swapped = True
            real_rename(candidate, saved_created)
            real_rename(replacement, candidate)
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(profiles, "_write_profile_auth_authority", fail_authority_write)
    supported_dir_fd = set(profiles.os.supports_dir_fd)
    supported_dir_fd.discard(real_rmdir)
    supported_dir_fd.add(swap_inside_final_rmdir)
    monkeypatch.setattr(profiles.os, "rmdir", swap_inside_final_rmdir)
    monkeypatch.setattr(profiles.os, "supports_dir_fd", supported_dir_fd)

    with pytest.raises(ValueError, match="forced authority failure"):
        profiles.create_profile("posix-rmdir-race", auth_mode="shared", no_alias=True)

    assert not swapped, "rollback must not issue a path-bound final rmdir"
    assert replacement.is_dir()


def test_portable_rollback_never_rmtrees_a_replaced_profile(
    profile_root, tmp_path, monkeypatch
):
    import hermes_cli.profiles as profiles

    target = profile_root / "profiles" / "portable-rmtree-race"
    saved_created = profile_root / "profiles" / ".portable-rmtree-created"
    replacement = tmp_path / "portable-external-directory"
    replacement.mkdir()
    sentinel = replacement / "sentinel.txt"
    sentinel.write_text("external sentinel", encoding="utf-8")
    real_rmtree = profiles.shutil.rmtree
    real_rename = profiles.os.rename
    swapped = False

    def fail_authority_write(*_args, **_kwargs):
        raise ValueError("forced authority failure")

    def swap_inside_rmtree(path, *args, **kwargs):
        nonlocal swapped
        candidate = Path(path)
        if not swapped and ".rollback-" in candidate.name:
            swapped = True
            real_rename(candidate, saved_created)
            real_rename(replacement, candidate)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(profiles, "_write_profile_auth_authority", fail_authority_write)
    monkeypatch.setattr(profiles.os, "supports_dir_fd", set())
    monkeypatch.setattr(profiles.shutil, "rmtree", swap_inside_rmtree)

    with pytest.raises((ValueError, RuntimeError), match="forced|secure directory"):
        profiles.create_profile("portable-rmtree-race", auth_mode="shared", no_alias=True)

    assert not swapped, "rollback must not recursively delete by pathname"
    assert sentinel.read_text(encoding="utf-8") == "external sentinel"


def test_clone_all_description_rejects_symlinked_profile_metadata(
    profile_root, tmp_path
):
    import hermes_cli.profiles as profiles

    external = tmp_path / "external-profile.yaml"
    sentinel = b"description: external sentinel\n"
    external.write_bytes(sentinel)
    (profile_root / "profile.yaml").symlink_to(external)

    with pytest.raises(ValueError, match="profile.yaml.*regular file"):
        profiles.create_profile(
            "meta-symlink",
            clone_all=True,
            auth_mode="shared",
            description="must not escape staging",
            no_alias=True,
        )

    assert external.read_bytes() == sentinel
    assert not os.path.lexists(profile_root / "profiles" / "meta-symlink")


def test_clone_profile_during_source_rotation_never_copies_oauth_chain(
    profile_root, monkeypatch
):
    import hermes_cli.profiles as profiles

    source_config = profile_root / "config.yaml"
    source_config.write_text("display:\n  skin: mono\n", encoding="utf-8")
    source_auth = profile_root / "auth.json"
    source_auth.write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "access_token": "access-before",
                        "refresh_token": "refresh-before",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    clone_started = threading.Event()
    allow_clone = threading.Event()
    real_copy2 = profiles.shutil.copy2

    def pause_config_copy(source, destination, *args, **kwargs):
        if Path(source) == source_config:
            clone_started.set()
            assert allow_clone.wait(timeout=5)
        return real_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(profiles.shutil, "copy2", pause_config_copy)
    outcome: dict[str, object] = {}

    def clone() -> None:
        try:
            outcome["profile"] = profiles.create_profile(
                "racing",
                clone_from="default",
                auth_mode="profile",
                no_alias=True,
            )
        except BaseException as exc:  # surfaced in the parent thread below
            outcome["error"] = exc

    worker = threading.Thread(target=clone)
    worker.start()
    assert clone_started.wait(timeout=5)
    source_auth.write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "access_token": "access-after",
                        "refresh_token": "refresh-after",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    allow_clone.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    local = Path(outcome["profile"])  # type: ignore[arg-type]
    local_auth = json.loads((local / "auth.json").read_text(encoding="utf-8"))
    assert local_auth["providers"] == {}
    assert "refresh-before" not in (local / "auth.json").read_text(encoding="utf-8")
    assert "refresh-after" not in (local / "auth.json").read_text(encoding="utf-8")
    assert json.loads(source_auth.read_text(encoding="utf-8"))["providers"][
        "openai-codex"
    ]["refresh_token"] == "refresh-after"


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
