#!/usr/bin/env python3
"""Resolve the auth-store path during early container boot.

This mirrors hermes_cli.auth_authority without importing the application and
uses the canonical strict YAML authority loader shared with Nix activation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable

import fcntl

try:
    from scripts.auth_authority_config import load_configured_authority
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from auth_authority_config import load_configured_authority


def _root_and_profile(home: Path) -> tuple[Path, str]:
    if home.parent.name == "profiles" and home.parent.parent.name == ".hermes":
        return home.parent.parent, home.name
    return home, "default"


def resolve_auth_authority(hermes_home: str) -> dict[str, Any]:
    home = Path(hermes_home).expanduser().resolve(strict=False)
    root, profile_id = _root_and_profile(home)
    requested = load_configured_authority(home / "config.yaml")
    legacy = False
    if requested is None:
        if profile_id != "default" and (home / "auth.json").is_file():
            authority = "profile"
            legacy = True
        else:
            authority = "shared"
    else:
        authority = requested

    if authority == "shared":
        path = root / "auth.json"
    elif authority == "profile":
        path = home / "auth.json"
    else:
        raise ValueError(f"Invalid auth.authority {authority!r}; expected shared or profile")

    bridged = os.environ.get("HERMES_INTERNAL_AUTHORITY_PATH", "").strip()
    if bridged:
        bridge_path = Path(bridged).expanduser()
        if not bridge_path.is_absolute():
            raise ValueError("HERMES_INTERNAL_AUTHORITY_PATH must be absolute")
        bridge_path = bridge_path.resolve(strict=False)
        try:
            bridge_path.relative_to(root.resolve(strict=False))
        except ValueError as exc:
            raise ValueError(
                "HERMES_INTERNAL_AUTHORITY_PATH must remain inside the Hermes root"
            ) from exc
        if bridge_path != path.resolve(strict=False):
            raise ValueError(
                "HERMES_INTERNAL_AUTHORITY_PATH does not match the configured auth authority"
            )

    return {
        "authority": authority,
        "requested_authority": requested,
        "auth_path": str(path),
        "lock_path": str(path.with_suffix(".lock")),
        "profile_id": profile_id,
        "legacy_compatibility": legacy,
    }


def _atomic_json_write(auth_path: Path, value: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".auth-update-", dir=auth_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, auth_path)
        os.chmod(auth_path, 0o600)
        directory_fd = os.open(auth_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def update_auth_store(
    hermes_home: str | Path,
    updater: Callable[[dict[str, Any]], tuple[str, dict[str, Any] | None]],
    *,
    expected_auth_path: str | Path | None = None,
) -> str:
    """Lock, reread, and optionally replace the selected canonical auth store."""
    resolved = resolve_auth_authority(str(hermes_home))
    auth_path = Path(resolved["auth_path"])
    lock_path = Path(resolved["lock_path"])
    if expected_auth_path is not None and auth_path.resolve(strict=False) != Path(
        expected_auth_path
    ).resolve(strict=False):
        raise RuntimeError("auth authority changed before update")
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    lock_flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        locked = resolve_auth_authority(str(hermes_home))
        if Path(locked["auth_path"]).resolve(strict=False) != auth_path.resolve(
            strict=False
        ):
            raise RuntimeError("auth authority changed while waiting for the lock")
        if auth_path.is_symlink():
            raise RuntimeError("refusing symlinked auth destination")
        if not auth_path.is_file():
            return "no_auth_file"
        try:
            store = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "auth_unreadable"
        if not isinstance(store, dict):
            return "auth_unreadable"
        status, updated = updater(store)
        if updated is not None:
            _atomic_json_write(auth_path, updated)
        return status
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def seed_auth_store(hermes_home: str, raw: str) -> str:
    """Create the selected store once while holding the canonical auth lock."""
    if not raw:
        return "no_seed"
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("auth bootstrap must be a JSON object")
    resolved = resolve_auth_authority(hermes_home)
    auth_path = Path(resolved["auth_path"])
    lock_path = Path(resolved["lock_path"])
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    lock_flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        locked = resolve_auth_authority(hermes_home)
        if Path(locked["auth_path"]).resolve(strict=False) != auth_path.resolve(
            strict=False
        ):
            raise ValueError("auth authority changed while waiting for the lock")
        if auth_path.is_symlink():
            raise ValueError("refusing symlinked auth bootstrap destination")
        if auth_path.exists():
            return "exists"
        fd, tmp_name = tempfile.mkstemp(
            prefix=".auth-bootstrap-", dir=auth_path.parent
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(parsed, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, auth_path)
            os.chmod(auth_path, 0o600)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return "seeded"
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main() -> int:
    home = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HERMES_HOME", "")
    if not home:
        print("HERMES_HOME is required", file=sys.stderr)
        return 2
    try:
        result = resolve_auth_authority(home)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    field = sys.argv[2] if len(sys.argv) > 2 else None
    if field == "seed":
        try:
            print(
                seed_auth_store(
                    home, os.environ.get("HERMES_AUTH_JSON_BOOTSTRAP", "")
                )
            )
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    if field:
        if field not in result:
            print(f"Unknown result field: {field}", file=sys.stderr)
            return 2
        print(result[field])
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
