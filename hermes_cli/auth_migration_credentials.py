"""Promote profile-owned PKCE credentials into the shared pool's existing owner.

The original singleton stays untouched for rollback. The migrated row uses
``manual:hermes_pkce``, whose refresh transaction commits to auth.json, so the
shared pool never depends on a file in a former profile authority.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


def profile_credential_artifacts(homes: Iterable[Path]) -> set[Path]:
    from agent.anthropic_credentials import _spent_rotation_sidecar_path

    singletons = {home / ".anthropic_oauth.json" for home in homes}
    return singletons | {_spent_rotation_sidecar_path(path) for path in singletons}


def migration_source_store(home: Path) -> dict[str, Any]:
    """Read one source under migration locks, adopting its backing token state."""
    from agent.anthropic_credentials import is_rotation_consumed_uncommitted
    from hermes_cli.auth_migration import AuthMigrationError, _read_json_object

    store = _read_json_object(home / "auth.json")
    pool = store.get("credential_pool") or {}
    entries = pool.get("anthropic") or []
    rows = [row for row in entries if isinstance(row, dict) and row.get("source") == "hermes_pkce"]
    singleton = home / ".anthropic_oauth.json"
    if not rows and not singleton.exists():
        return store
    if len(rows) > 1:
        raise AuthMigrationError(f"Profile {home.name} has ambiguous Hermes OAuth rows; repair its Anthropic pool first")
    if not singleton.is_file() or singleton.is_symlink():
        raise AuthMigrationError(f"Profile {home.name} has no regular Hermes OAuth backing file; re-authenticate Anthropic first")
    credentials = _read_json_object(singleton)
    access = credentials.get("accessToken")
    refresh = credentials.get("refreshToken")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise AuthMigrationError(f"Profile {home.name} has incomplete Hermes OAuth credentials; re-authenticate Anthropic first")
    if any(is_rotation_consumed_uncommitted(secret, source_path=singleton) for secret in (access, refresh)):
        raise AuthMigrationError(f"Profile {home.name} has an uncommitted OAuth rotation; re-authenticate Anthropic first")
    if not rows and any(
        isinstance(row, dict) and row.get("access_token") == access and row.get("refresh_token") == refresh
        for row in entries
    ):
        return store
    migrated = {
        **(rows[0] if rows else {"id": "migrated-hermes-pkce", "label": "Hermes OAuth", "priority": len(entries)}),
        "source": "manual:hermes_pkce", "auth_type": "oauth", "access_token": access,
        "refresh_token": refresh, "expires_at_ms": credentials.get("expiresAt"),
    }
    store.setdefault("credential_pool", {})["anthropic"] = [
        migrated if row is rows[0] else row for row in entries
    ] if rows else [*entries, migrated]
    return store
