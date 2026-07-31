#!/usr/bin/env python3
"""Authority-aware, lock-safe auth.json seeding for NixOS activation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

try:
    from scripts.auth_authority_config import load_configured_authority
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from auth_authority_config import load_configured_authority


def _shared_root(home: Path) -> Path:
    resolved = home.expanduser().resolve(strict=False)
    if resolved.parent.name == "profiles":
        return resolved.parent.parent
    return resolved


def _configured_authority(home: Path) -> str | None:
    return load_configured_authority(home / "config.yaml")


def resolve_auth_authority(home: Path) -> dict[str, Any]:
    home = home.expanduser().resolve(strict=False)
    root = _shared_root(home)
    configured = _configured_authority(home)
    requested = configured or "shared"
    if requested not in {"shared", "profile"}:
        raise ValueError(
            "Invalid auth.authority in config.yaml; expected 'shared' or 'profile'"
        )
    profile_path = home / "auth.json"
    legacy = (
        configured is None
        and home != root
        and profile_path.is_file()
    )
    effective = "profile" if requested == "profile" or legacy else "shared"
    auth_path = profile_path if effective == "profile" else root / "auth.json"
    return {
        "requested_authority": requested,
        "authority": effective,
        "auth_path": auth_path,
        "lock_path": auth_path.with_suffix(".lock"),
        "legacy_compatibility": legacy,
    }


def _verify_private_file(
    path: Path,
    *,
    label: str,
    uid: int | None,
    gid: int | None,
) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(f"cannot verify {label}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError(f"{label} must have mode 0600")
    if uid is not None and metadata.st_uid != uid:
        raise RuntimeError(f"{label} is not owned by runtime uid {uid}")
    if gid is not None and metadata.st_gid != gid:
        raise RuntimeError(f"{label} is not owned by runtime gid {gid}")


def seed_auth(
    home: Path,
    source: Path,
    *,
    uid: int | None = None,
    gid: int | None = None,
) -> dict[str, Any]:
    """Seed an empty authority while serializing existence checks and writes."""
    source = source.expanduser()
    if source.is_symlink():
        raise RuntimeError("auth seed source must not be a symlink")
    source = source.resolve(strict=True)
    if not source.is_file():
        raise RuntimeError("auth seed source must be a regular file")
    authority = resolve_auth_authority(home)
    destination = authority["auth_path"]
    lock_path = authority["lock_path"]
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if uid is not None or gid is not None:
        os.chown(destination.parent, -1 if uid is None else uid, -1 if gid is None else gid)

    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    try:
        os.fchmod(lock_fd, stat.S_IRUSR | stat.S_IWUSR)
        if uid is not None or gid is not None:
            os.fchown(lock_fd, -1 if uid is None else uid, -1 if gid is None else gid)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if destination.is_symlink():
            raise RuntimeError("refusing symlinked auth seed destination")
        if destination.exists() and not destination.is_file():
            raise RuntimeError("auth seed destination must be a regular file")
        existed = destination.is_file()
        if existed:
            status = "preserved"
        else:
            try:
                seed_value = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"auth seed is not valid JSON: {exc}") from exc
            if not isinstance(seed_value, dict):
                raise RuntimeError("auth seed must be a JSON object")
            seed_raw = (json.dumps(seed_value, separators=(",", ":")) + "\n").encode()
            fd, tmp_name = tempfile.mkstemp(
                prefix=f"{destination.name}.tmp.", dir=destination.parent
            )
            tmp_path = Path(tmp_name)
            try:
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                if uid is not None or gid is not None:
                    os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
                with os.fdopen(fd, "wb") as output:
                    output.write(seed_raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(tmp_path, destination)
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                tmp_path.unlink(missing_ok=True)
            status = "created"
        _verify_private_file(
            destination, label="auth seed destination", uid=uid, gid=gid
        )
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise RuntimeError("auth seed lock must be a regular file")
        if stat.S_IMODE(lock_stat.st_mode) != 0o600:
            raise RuntimeError("auth seed lock must have mode 0600")
        if uid is not None and lock_stat.st_uid != uid:
            raise RuntimeError(f"auth seed lock is not owned by runtime uid {uid}")
        if gid is not None and lock_stat.st_gid != gid:
            raise RuntimeError(f"auth seed lock is not owned by runtime gid {gid}")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    return {
        "status": status,
        "authority": authority["authority"],
        "legacy_compatibility": authority["legacy_compatibility"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("home", type=Path)
    parser.add_argument("source", type=Path)

    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    args = parser.parse_args()
    print(
        json.dumps(
            seed_auth(
                args.home,
                args.source,
                uid=args.uid,
                gid=args.gid,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
