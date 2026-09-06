"""Pin authentication authority while its topology and stores are locked.

The current auth module owns the cross-platform file-lock primitive and its
per-path reentrancy tracking. This module composes those locks for migrations
and profile-aware credential transactions.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, Optional

from hermes_constants import get_default_hermes_root, get_hermes_home, secure_parent_dir
from hermes_cli.auth_constants import AUTH_LOCK_TIMEOUT_SECONDS


@dataclass(frozen=True)
class AuthTransaction:
    profile_home: Path
    path: Path
    fallback_path: Optional[Path]
    locked_paths: frozenset[Path]


_transaction: ContextVar[Optional[AuthTransaction]] = ContextVar("auth_transaction", default=None)


def current_auth_transaction() -> Optional[AuthTransaction]:
    """A pin belongs to one profile context, even when contexts share a thread."""
    current = _transaction.get()
    if current is not None and current.profile_home == get_hermes_home().resolve(strict=False):
        return current
    return None


def _validate_locked_store_path(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink auth store path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_parent_dir(path)


@contextmanager
def _auth_transition_lock(timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS):
    """Take the topology lock before resolving or locking credential stores."""
    from hermes_cli import auth

    lock_path = get_default_hermes_root().resolve(strict=False) / "auth-transition.lock"
    with auth._file_lock(lock_path, auth._auth_lock_holder_for(lock_path), timeout_seconds,
                         "Timed out waiting for auth authority transition lock"):
        yield


@contextmanager
def _auth_store_locks(
    target_paths: Optional[Iterable[Path]] = None, *,
    transaction_target: Optional[Path] = None,
    include_legacy_fallback: bool = False,
    timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS,
):
    """Lock the authority and every store in canonical order, restoring outer pins."""
    from hermes_cli import auth

    with _auth_transition_lock(timeout_seconds), ExitStack() as locks:
        fallback = None
        if target_paths is None:
            active = auth._auth_file_path().resolve(strict=False)
            paths = {active}
            if include_legacy_fallback:
                fallback = auth._global_auth_file_path()
                if fallback is not None:
                    fallback = fallback.resolve(strict=False)
                    paths.add(fallback)
        else:
            raw_paths = {Path(path) for path in target_paths}
            if not raw_paths:
                raise ValueError("at least one auth-store target is required")
            for path in raw_paths:
                _validate_locked_store_path(path)
            paths = {path.resolve(strict=False) for path in raw_paths}
            active = Path(transaction_target or min(paths, key=lambda p: os.fsencode(str(p)))).resolve(strict=False)
            # Explicit write-through locks preserve the caller's active-store
            # identity. Lock that store too rather than pin an unlocked target.
            paths.add(active)
            previous = current_auth_transaction()
            if previous is not None and previous.path == active:
                fallback = previous.fallback_path

        for path in sorted(paths, key=lambda p: os.fsencode(str(p))):
            _validate_locked_store_path(path)
            locks.enter_context(auth._file_lock(
                path.with_suffix(".lock"), auth._auth_lock_holder_for(path), timeout_seconds,
                "Timed out waiting for auth store lock"))
        token = _transaction.set(AuthTransaction(get_hermes_home().resolve(strict=False), active, fallback, frozenset(paths)))
        try:
            yield active, fallback
        finally:
            _transaction.reset(token)
