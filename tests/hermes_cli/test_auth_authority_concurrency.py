from __future__ import annotations

import json
import multiprocessing
import os
from contextlib import contextmanager
from pathlib import Path
import threading

import pytest


def _write_provider(profile_home: str, provider: str, ready, start) -> None:
    os.environ["HERMES_HOME"] = profile_home
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store

    ready.put(provider)
    start.wait(10)
    for sequence in range(20):
        with _auth_store_lock():
            store = _load_auth_store()
            store.setdefault("providers", {})[provider] = {"sequence": sequence}
            _save_auth_store(store)


def _increment_provider(
    profile_home: str, provider: str, iterations: int, ready, start
) -> None:
    os.environ["HERMES_HOME"] = profile_home
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store

    ready.put(profile_home)
    start.wait(10)
    for _ in range(iterations):
        with _auth_store_lock():
            store = _load_auth_store()
            state = store.setdefault("providers", {}).setdefault(provider, {})
            state["rotations"] = int(state.get("rotations", 0)) + 1
            _save_auth_store(store)


def _crash_while_locked(profile_home: str, acquired) -> None:
    os.environ["HERMES_HOME"] = profile_home
    from hermes_cli.auth import _auth_store_lock
    from hermes_cli.auth_authority import get_auth_store_path

    with _auth_store_lock(target_path=get_auth_store_path()):
        acquired.set()
        os._exit(23)


def test_shared_authority_serializes_writers_from_distinct_profiles(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    profiles = [root / "profiles" / name for name in ("alpha", "beta")]
    for profile in profiles:
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(
            "auth:\n  authority: shared\n", encoding="utf-8"
        )

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    workers = [
        context.Process(
            target=_write_provider,
            args=(str(profile), provider, ready, start),
        )
        for profile, provider in zip(profiles, ("alpha", "beta"), strict=True)
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {"alpha", "beta"}
    start.set()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0

    store = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    assert store["providers"]["alpha"]["sequence"] == 19
    assert store["providers"]["beta"]["sequence"] == 19
    assert (root / "auth.json").stat().st_mode & 0o777 == 0o600


def test_shared_authority_lock_is_released_when_writer_crashes(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    profile = root / "profiles" / "crash"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "auth:\n  authority: shared\n", encoding="utf-8"
    )

    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    crashed = context.Process(
        target=_crash_while_locked, args=(str(profile), acquired)
    )
    crashed.start()
    assert acquired.wait(10)
    crashed.join(10)
    assert crashed.exitcode == 23

    os.environ["HERMES_HOME"] = str(profile)
    from hermes_cli.auth import _auth_store_lock, _save_auth_store
    from hermes_cli.auth_authority import get_auth_store_path

    path = get_auth_store_path()
    with _auth_store_lock(target_path=path, timeout_seconds=2):
        _save_auth_store({"providers": {"after-crash": {}}}, target_path=path)

    assert "after-crash" in json.loads(path.read_text())["providers"]


def test_shared_authority_serializes_same_provider_rotation(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    profiles = [root / "profiles" / name for name in ("alpha", "beta")]
    for profile in profiles:
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(
            "auth:\n  authority: shared\n", encoding="utf-8"
        )

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    workers = [
        context.Process(
            target=_increment_provider,
            args=(str(profile), "nous", 20, ready, start),
        )
        for profile in profiles
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {
        str(profile) for profile in profiles
    }
    start.set()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0

    store = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    assert store["providers"]["nous"]["rotations"] == 40
    assert (root / "auth.lock").is_file()
    assert not any((profile / "auth.lock").exists() for profile in profiles)


def test_explicit_profile_authorities_remain_isolated_under_same_workload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hermes"
    profiles = [root / "profiles" / name for name in ("alpha", "beta")]
    for profile in profiles:
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(
            "auth:\n  authority: profile\n", encoding="utf-8"
        )

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    workers = [
        context.Process(
            target=_increment_provider,
            args=(str(profile), "nous", 20, ready, start),
        )
        for profile in profiles
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {
        str(profile) for profile in profiles
    }
    start.set()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0

    for profile in profiles:
        store = json.loads((profile / "auth.json").read_text(encoding="utf-8"))
        assert store["providers"]["nous"]["rotations"] == 20
        assert (profile / "auth.json").stat().st_mode & 0o777 == 0o600
        assert (profile / "auth.lock").is_file()
    assert not (root / "auth.json").exists()
    assert not (root / "auth.lock").exists()


@pytest.mark.parametrize("failure_point", ["before-temp", "after-temp"])
def test_atomic_save_failure_preserves_store_and_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import hermes_cli.auth as auth_mod

    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "auth:\n  authority: shared\n", encoding="utf-8"
    )
    auth_file = home / "auth.json"
    original = b'{"version":1,"providers":{"nous":{"generation":"old"}}}\n'
    auth_file.write_bytes(original)
    auth_file.chmod(0o600)
    backup = home / "auth.json.backup"
    backup.write_bytes(original)
    backup.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(home))

    if failure_point == "before-temp":
        real_open = auth_mod.os.open

        def fail_temp_open(path, flags, mode=0o777):
            if ".tmp." in os.fspath(path):
                raise OSError("failure before temporary-file creation")
            return real_open(path, flags, mode)

        monkeypatch.setattr(auth_mod.os, "open", fail_temp_open)
    else:
        def fail_replace(*_args):
            raise OSError("failure after temporary-file creation")

        monkeypatch.setattr(auth_mod, "atomic_replace", fail_replace)

    with pytest.raises(OSError, match="temporary-file creation"):
        with auth_mod._auth_store_lock():
            auth_mod._save_auth_store(
                {"providers": {"nous": {"generation": "new"}}}
            )

    assert json.loads(auth_file.read_text(encoding="utf-8"))["providers"]["nous"] == {
        "generation": "old"
    }
    assert json.loads(backup.read_text(encoding="utf-8"))["providers"]["nous"] == {
        "generation": "old"
    }
    assert not list(home.glob("auth.json.tmp.*"))
    assert auth_file.stat().st_mode & 0o777 == 0o600


def test_store_symlink_substitution_while_acquiring_lock_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.auth as auth_mod

    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "auth:\n  authority: shared\n", encoding="utf-8"
    )
    outside = tmp_path / "outside.json"
    outside.write_text('{"providers":{"outside":{}}}', encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    real_file_lock = auth_mod._file_lock
    substituted = False

    @contextmanager
    def substitute_after_lock(lock_path, holder, timeout_seconds, timeout_message):
        nonlocal substituted
        with real_file_lock(lock_path, holder, timeout_seconds, timeout_message):
            if Path(lock_path) == home / "auth.lock" and not substituted:
                substituted = True
                (home / "auth.json").symlink_to(outside)
            yield

    monkeypatch.setattr(auth_mod, "_file_lock", substitute_after_lock)

    with pytest.raises(RuntimeError, match="symlink auth transaction path"):
        with auth_mod._auth_store_lock():
            auth_mod._save_auth_store({"providers": {"replacement": {}}})

    assert substituted is True
    assert (home / "auth.json").is_symlink()
    assert json.loads(outside.read_text(encoding="utf-8")) == {
        "providers": {"outside": {}}
    }


def test_implicit_auth_transaction_pins_the_resolved_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A topology change cannot split one locked load/modify/save transaction."""
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store
    from hermes_cli.auth_authority import AuthAuthority

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text('{"providers": {"nous": {"value": "first"}}}', encoding="utf-8")
    second.write_text('{"providers": {"nous": {"value": "second"}}}', encoding="utf-8")
    calls = 0

    def make_authority(path: Path) -> AuthAuthority:
        return AuthAuthority(
            requested_mode="shared",
            effective_mode="shared",
            auth_path=path,
            lock_path=path.with_suffix(".lock"),
            profile_home=tmp_path,
            shared_root=tmp_path,
            profile_id=None,
            config_path=tmp_path / "config.yaml",
        )

    def changing_resolver(**_kwargs) -> AuthAuthority:
        nonlocal calls
        calls += 1
        return make_authority(first if calls == 1 else second)

    monkeypatch.setattr("hermes_cli.auth.resolve_auth_authority", changing_resolver)

    with _auth_store_lock():
        store = _load_auth_store()
        store["providers"]["nous"]["value"] = "updated"
        _save_auth_store(store)

    assert json.loads(first.read_text(encoding="utf-8"))["providers"]["nous"]["value"] == "updated"
    assert json.loads(second.read_text(encoding="utf-8"))["providers"]["nous"]["value"] == "second"


def test_nested_auth_transaction_does_not_invert_transition_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reentrant auth lock must not wait behind a transition waiting on it."""
    from hermes_cli.auth import _auth_store_lock, _auth_transition_lock

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "auth:\n  authority: profile\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))
    transition_acquired = threading.Event()

    def transition() -> None:
        with _auth_transition_lock(timeout_seconds=2):
            transition_acquired.set()
            with _auth_store_lock(
                target_path=profile / "auth.json", timeout_seconds=2
            ):
                pass

    with _auth_store_lock(timeout_seconds=2):
        worker = threading.Thread(target=transition)
        worker.start()
        assert transition_acquired.wait(1)
        with _auth_store_lock(timeout_seconds=2):
            pass
        assert worker.is_alive()

    worker.join(2)
    assert not worker.is_alive()
    assert transition_acquired.is_set()
