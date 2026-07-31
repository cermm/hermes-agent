"""Canonical fail-closed auth.authority loader for bootstrap scripts."""

from __future__ import annotations

from pathlib import Path

import yaml

_VALID_AUTHORITIES = {"shared", "profile"}


class AuthorityConfigError(RuntimeError, ValueError):
    """Raised when auth.authority cannot be resolved safely."""


def load_configured_authority(config_path: Path) -> str | None:
    """Load auth.authority with the same YAML mapping contract as Hermes core."""
    if not config_path.is_file():
        return None
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise AuthorityConfigError(
            f"invalid auth authority config at {config_path}: {exc}"
        ) from exc

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AuthorityConfigError(
            f"invalid auth authority config at {config_path}: root must be a mapping"
        )
    auth = raw.get("auth")
    if auth is None:
        return None
    if not isinstance(auth, dict):
        raise AuthorityConfigError(
            f"invalid auth authority config at {config_path}: auth must be a mapping"
        )
    mode = auth.get("authority")
    if mode is None:
        return None
    if not isinstance(mode, str) or mode.strip().lower() not in _VALID_AUTHORITIES:
        raise AuthorityConfigError(
            f"Invalid auth.authority in auth authority config at {config_path}: "
            "auth.authority must be 'shared' or 'profile'"
        )
    return mode.strip().lower()
