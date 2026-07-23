from __future__ import annotations

import json
import multiprocessing
import os
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
