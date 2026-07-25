"""Canonical resolver for Hermes' authentication-store authority.

The active profile still owns configuration, but authentication may be shared
across profiles or isolated to one profile.
Every auth.json reader/writer must resolve through this module so the data file
and its advisory lock cannot diverge.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Optional

from hermes_constants import get_default_hermes_root, get_hermes_home
from utils import fast_safe_load


AUTH_MODES = frozenset({"shared", "profile"})


class AuthAuthorityConfigError(RuntimeError):
    """Raised when auth authority configuration is malformed or incomplete."""


_TERMINAL_MIGRATION_PHASES = frozenset(
    {"committed", "committed_state_changed", "rolled_back", "aborted"}
)


def _newest_journal_candidates(journals: Path) -> list[Path]:
    """Return newest-first journals, retaining unreadable entries to fail closed."""
    candidates: list[tuple[int | None, Path]] = []
    try:
        paths = list(journals.glob("*.json"))
    except OSError:
        return [journals / "unreadable.json"]
    for path in paths:
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            candidates.append((None, path))
    candidates.sort(
        key=lambda item: (item[0] is None, item[0] or 0),
        reverse=True,
    )
    return [item[1] for item in candidates]

def incomplete_auth_migration(shared_root: Path) -> Optional[dict[str, str]]:
    """Return the newest incomplete migration journal without exposing secrets."""
    journals = shared_root / "state-snapshots" / "auth-migrations" / "journals"
    for path in _newest_journal_candidates(journals):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"plan_id": path.stem, "phase": "unreadable"}
        if not isinstance(raw, dict):
            return {"plan_id": path.stem, "phase": "malformed"}
        phase = str(raw.get("phase") or "unknown")
        if phase not in _TERMINAL_MIGRATION_PHASES:
            return {"plan_id": str(raw.get("plan_id") or path.stem), "phase": phase}
    return None


_TERMINAL_RESTORE_PHASES = frozenset({"committed", "rolled_back", "aborted"})


def incomplete_auth_restore(shared_root: Path) -> Optional[dict[str, str]]:
    """Return the newest interrupted auth/config restore journal."""
    journals = shared_root / "state-snapshots" / "auth-restores" / "journals"
    for path in _newest_journal_candidates(journals):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"operation_id": path.stem, "phase": "unreadable"}
        if not isinstance(raw, dict):
            return {"operation_id": path.stem, "phase": "malformed"}
        phase = str(raw.get("phase") or "unknown")
        if phase not in _TERMINAL_RESTORE_PHASES:
            return {
                "operation_id": str(raw.get("operation_id") or path.stem),
                "phase": phase,
            }
    return None


@dataclass(frozen=True)
class AuthAuthority:
    """Resolved authentication authority and non-secret diagnostic metadata."""

    requested_mode: str
    effective_mode: str
    auth_path: Path
    lock_path: Path
    profile_home: Path
    shared_root: Path
    profile_id: Optional[str]
    config_path: Path
    legacy_compatibility: bool = False
    conflicting_store: Optional[Path] = None

    @property
    def provenance(self) -> str:
        if self.legacy_compatibility:
            return "legacy-profile-store"
        if self.effective_mode == "shared":
            return "shared-root"
        if self.effective_mode == "profile":
            return f"profile:{self.profile_id or 'default'}"
        raise AssertionError(f"unsupported auth authority mode: {self.effective_mode}")


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except Exception:
        return left == right


def _profile_id(profile_home: Path, shared_root: Path) -> Optional[str]:
    if profile_home.parent.name == "profiles" and _same_path(
        profile_home.parent.parent, shared_root
    ):
        return profile_home.name
    return None


def _read_authority_config(config_path: Path) -> tuple[Mapping[str, Any], bool]:
    """Return the raw ``auth`` section and whether ``auth.authority`` was explicit.

    Unlike the best-effort general config reader, authority resolution fails
    closed: silently treating malformed YAML as the default could make a writer
    mutate a different credential store than the operator intended.
    """
    if not config_path.exists():
        return {}, False
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = fast_safe_load(handle) or {}
    except Exception as exc:
        raise AuthAuthorityConfigError(
            f"Cannot resolve authentication authority because {config_path} "
            f"is unreadable or invalid: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise AuthAuthorityConfigError(
            f"Cannot resolve authentication authority: {config_path} must contain a mapping."
        )
    section = raw.get("auth")
    if section is None:
        return {}, False
    if not isinstance(section, dict):
        raise AuthAuthorityConfigError("auth must be a mapping in config.yaml")
    return section, "authority" in section


def resolve_auth_authority(
    *,
    profile_home: Optional[Path] = None,
    shared_root: Optional[Path] = None,
    config: Optional[Mapping[str, Any]] = None,
    enforce_migration: bool = True,
    enforce_restore: bool = True,
) -> AuthAuthority:
    """Resolve the one authoritative auth store for the current process.

    ``shared`` is the default for new installs. For compatibility, an existing
    profile-local auth.json remains authoritative while ``auth.authority`` is absent;
    setting a mode explicitly exits that compatibility path.
    """
    active_home = Path(profile_home or get_hermes_home()).expanduser()
    root = Path(shared_root or get_default_hermes_root()).expanduser()
    pending = incomplete_auth_migration(root)
    if enforce_migration and pending:
        raise AuthAuthorityConfigError(
            "Authentication is blocked by incomplete migration "
            f"{pending['plan_id']} ({pending['phase']}); run "
            f"`hermes auth migrate-shared --recover --plan-id {pending['plan_id']}`"
        )
    pending_restore = incomplete_auth_restore(root)
    if enforce_restore and pending_restore:
        raise AuthAuthorityConfigError(
            "Authentication is blocked by incomplete restore "
            f"{pending_restore['operation_id']} ({pending_restore['phase']}); rerun the "
            "same snapshot restore command while gateways are stopped"
        )
    config_path = active_home / "config.yaml"

    if config is None:
        section, explicit_mode = _read_authority_config(config_path)
    else:
        raw_section = config.get("auth") if "auth" in config else config
        if raw_section is None:
            section = {}
        elif not isinstance(raw_section, Mapping):
            raise AuthAuthorityConfigError("auth must be a mapping in config.yaml")
        else:
            section = raw_section
        explicit_mode = "authority" in section

    raw_mode = section.get("authority", "shared")
    if not isinstance(raw_mode, str):
        raise AuthAuthorityConfigError(
            "auth.authority must be shared or profile"
        )
    requested_mode = raw_mode.strip().lower()
    if requested_mode not in AUTH_MODES:
        raise AuthAuthorityConfigError(
            f"Invalid auth.authority {raw_mode!r}; expected shared or profile"
        )

    profile_path = active_home / "auth.json"
    shared_path = root / "auth.json"
    legacy = False

    if requested_mode == "profile":
        auth_path = profile_path
        effective_mode = "profile"
    else:
        auth_path = shared_path
        effective_mode = "shared"
        if (
            not explicit_mode
            and not _same_path(profile_path, shared_path)
            and profile_path.is_file()
        ):
            auth_path = profile_path
            effective_mode = "profile"
            legacy = True

    for candidate in (profile_path, shared_path):
        if candidate.is_symlink():
            raise AuthAuthorityConfigError(
                f"Authentication authority path must not be a symlink: {candidate}"
            )

    auth_path = auth_path.resolve(strict=False)
    if auth_path.exists() and not auth_path.is_file():
        raise AuthAuthorityConfigError(
            f"Authentication authority path must be a file, not {auth_path}"
        )
    lock_path = auth_path.with_suffix(".lock")

    conflicting_store: Optional[Path] = None
    for candidate in (profile_path, shared_path):
        if candidate.is_file() and not _same_path(candidate, auth_path):
            conflicting_store = candidate.resolve(strict=False)
            break

    return AuthAuthority(
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        auth_path=auth_path,
        lock_path=lock_path,
        profile_home=active_home.resolve(strict=False),
        shared_root=root.resolve(strict=False),
        profile_id=_profile_id(active_home, root),
        config_path=config_path.resolve(strict=False),
        legacy_compatibility=legacy,
        conflicting_store=conflicting_store,
    )


def get_auth_store_path() -> Path:
    """Return the canonical data-file path for authentication state."""
    return resolve_auth_authority().auth_path


def get_auth_lock_path() -> Path:
    """Return the lock path paired with the canonical authentication store."""
    return resolve_auth_authority().lock_path


def _display_authority_path(path: Path, shared_root: Path) -> str:
    """Render an authority path without exposing an operator-specific HOME."""
    try:
        relative = path.resolve(strict=False).relative_to(
            shared_root.resolve(strict=False)
        )
    except ValueError:
        relative = Path(path.name)
    return str(Path("~/.hermes") / relative)


def describe_auth_store() -> str:
    """Return a normalized, non-secret location label for user-facing errors."""
    authority = resolve_auth_authority(enforce_migration=False)
    location = _display_authority_path(authority.auth_path, authority.shared_root)
    if authority.legacy_compatibility:
        return f"legacy profile-local auth store ({location})"
    if authority.effective_mode == "shared":
        return f"shared auth store ({location})"
    return f"profile-local auth store ({location})"


def auth_authority_status(
    *,
    profile_home: Optional[Path] = None,
    shared_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Return redacted authority metadata for CLI/doctor diagnostics."""
    authority = resolve_auth_authority(
        profile_home=profile_home,
        shared_root=shared_root,
        enforce_migration=False,
    )
    migration = incomplete_auth_migration(authority.shared_root)
    exists = authority.auth_path.is_file()
    permissions: Optional[str] = None
    owner_ok: Optional[bool] = None
    writable = False
    if exists:
        try:
            file_stat = authority.auth_path.stat()
            permissions = stat.filemode(file_stat.st_mode)
            owner_ok = file_stat.st_uid == authority.auth_path.parent.stat().st_uid
            writable = bool(file_stat.st_mode & stat.S_IWUSR)
        except OSError:
            pass
    else:
        try:
            writable = authority.auth_path.parent.exists() and os.access(
                authority.auth_path.parent, os.W_OK
            )
        except OSError:
            writable = False
    return {
        "requested_mode": authority.requested_mode,
        "effective_mode": authority.effective_mode,
        "path": _display_authority_path(authority.auth_path, authority.shared_root),
        "lock_path": _display_authority_path(authority.lock_path, authority.shared_root),
        "profile_id": authority.profile_id,
        "provenance": authority.provenance,
        "exists": exists,
        "permissions": permissions,
        "owner_ok": owner_ok,
        "writable": writable,
        "legacy_compatibility": authority.legacy_compatibility,
        "conflicting_store": (
            _display_authority_path(authority.conflicting_store, authority.shared_root)
            if authority.conflicting_store
            else None
        ),
        "migration": migration,
    }
