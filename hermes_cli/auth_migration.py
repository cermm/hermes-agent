"""Dry-run-first migration of legacy profile auth stores to shared authority.

Public output contains only topology and provider names. Full credential hashes
are kept solely in mode-0600 plan artifacts and are never used as public plan
digests or printed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Iterable, Optional
import uuid
import yaml

from hermes_constants import get_default_hermes_root
from hermes_cli.auth import (
    AUTH_STORE_VERSION,
    _auth_store_locks,
    _auth_transition_lock,
    _load_auth_store,
    _save_auth_store,
)
from hermes_cli.auth_authority import resolve_auth_authority
from utils import IndentDumper, atomic_yaml_write, fast_safe_load


class AuthMigrationError(RuntimeError):
    """A migration precondition, conflict, or recovery check failed."""


@dataclass(frozen=True)
class MigrationPlan:
    plan_id: str
    plan_digest: str
    manifest: dict[str, Any]
    artifact_path: Path


_POLICIES = frozenset({"abort", "prefer-shared", "prefer-profile"})


def _root() -> Path:
    return get_default_hermes_root().resolve(strict=False)


def _state_dir() -> Path:
    return _root() / "state-snapshots" / "auth-migrations"


def _private_bytes_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _private_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AuthMigrationError(f"Unreadable auth store at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuthMigrationError(f"Auth store at {path} is not a JSON object")
    return value


def _content_precondition(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "sha256": None}
    if not path.is_file() or path.is_symlink():
        raise AuthMigrationError(f"Refusing non-regular or symlink auth path: {path}")
    raw = path.read_bytes()
    return {
        "exists": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "mtime_ns": path.stat().st_mtime_ns,
    }


def _raw_identity(raw: bytes) -> dict[str, Any]:
    return {
        "exists": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }


def _matches_identity(path: Path, expected: Optional[dict[str, Any]]) -> bool:
    if expected is None:
        return False
    current = _content_precondition(path)
    return all(
        current.get(key) == expected.get(key)
        for key in ("exists", "sha256", "size")
        if key in expected
    )


def _providers(store: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("providers", "credential_pool"):
        section = store.get(key)
        if isinstance(section, dict):
            names.update(str(name) for name in section)
    return names


def _selected_profiles(*, all_profiles: bool, profile: Optional[str]) -> list[Path]:
    if all_profiles == bool(profile):
        raise AuthMigrationError(
            "Choose exactly one of --all-profiles or --profile NAME"
        )
    profiles_root = _root() / "profiles"
    if profile:
        if Path(profile).name != profile or profile in {".", ".."}:
            raise AuthMigrationError("Invalid profile name")
        candidates = [profiles_root / profile]
    else:
        candidates = (
            sorted(
                (path for path in profiles_root.iterdir() if path.is_dir()),
                key=lambda path: path.name,
            )
            if profiles_root.exists()
            else []
        )
    profiles_root_resolved = profiles_root.resolve(strict=False)
    result: list[Path] = []
    for home in candidates:
        resolved_home = home.resolve(strict=False)
        try:
            resolved_home.relative_to(profiles_root_resolved)
        except ValueError as exc:
            raise AuthMigrationError(
                f"Profile {home.name!r} resolves outside the Hermes profiles root"
            ) from exc
        source = resolved_home / "auth.json"
        if source.is_file():
            result.append(resolved_home)
    return result


def _gateway_homes_for_target(
    selected_homes: Iterable[Path], target: Path
) -> list[Path]:
    """Enumerate selected and already-shared homes whose gateways can write target."""
    root = _root()
    selected = {home.resolve(strict=False) for home in selected_homes}
    candidates = {root, *selected}
    profiles_root = root / "profiles"
    if profiles_root.exists():
        candidates.update(
            path.resolve(strict=False)
            for path in profiles_root.iterdir()
            if path.is_dir()
        )
    relevant: set[Path] = set()
    target = target.resolve(strict=False)
    for home in candidates:
        try:
            authority = resolve_auth_authority(
                profile_home=home,
                shared_root=root,
                enforce_migration=False,
            )
        except Exception as exc:
            raise AuthMigrationError(
                f"Cannot determine auth authority for gateway home {home}"
            ) from exc
        if home in selected or authority.auth_path.resolve(strict=False) == target:
            relevant.add(home)
    return sorted(relevant, key=lambda path: os.fsencode(str(path)))


def _gateway_snapshot(profile_homes: Iterable[Path]) -> dict[str, Optional[int]]:
    """Capture live gateway PIDs without cleaning or mutating runtime state."""
    from gateway.status import get_running_pid, read_runtime_status
    try:
        from gateway.status import runtime_status_pid_is_live
    except ImportError:  # Older/current branches expose only lower-level PID probes.
        from gateway.status import _pid_exists

        def runtime_status_pid_is_live(runtime: dict[str, Any]) -> bool:
            raw_pid = runtime.get("pid")
            return isinstance(raw_pid, int) and raw_pid > 0 and _pid_exists(raw_pid)

    snapshot: dict[str, Optional[int]] = {}
    for home in profile_homes:
        pid = get_running_pid(home / "gateway.pid", cleanup_stale=False)
        if pid is None:
            runtime = read_runtime_status(home / "gateway_state.json")
            if (
                isinstance(runtime, dict)
                and runtime.get("gateway_state") in {"starting", "running", "degraded"}
                and runtime_status_pid_is_live(runtime)
            ):
                raw_pid = runtime.get("pid")
                if isinstance(raw_pid, int) and raw_pid > 0:
                    pid = raw_pid
        snapshot[str(home)] = pid
    return snapshot


def _redacted_manifest(profile_homes: Iterable[Path], target: Path) -> dict[str, Any]:
    target_store = _read_json_object(target) if target.exists() else {}
    sources: list[dict[str, Any]] = []
    target_providers = _providers(target_store)
    for home in profile_homes:
        source = home / "auth.json"
        config = home / "config.yaml"
        profile_id = resolve_auth_authority(
            profile_home=home,
            shared_root=_root(),
            enforce_migration=False,
        ).profile_id
        store = _read_json_object(source)
        providers = sorted(_providers(store))
        sources.append({
            "profile": home.name,
            "profile_id": profile_id,
            "source_class": "profile-local",
            "providers": providers,
            "overlapping_providers": sorted(set(providers) & target_providers),
            "artifacts": [
                {
                    "artifact_class": "profile-auth",
                    "exists": source.exists(),
                    "profile_id": profile_id,
                },
                {
                    "artifact_class": "profile-config",
                    "exists": config.exists(),
                    "profile_id": profile_id,
                },
            ],
        })
    return {
        "operation": "migrate-shared",
        "target_class": "shared-root",
        "target_exists": target.exists(),
        "target_artifact": {
            "artifact_class": "shared-auth",
            "exists": target.exists(),
        },
        "target_providers": sorted(target_providers),
        "sources": sources,
    }


def _manifest_digest(manifest: dict[str, Any]) -> str:
    public = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(public).hexdigest()


def plan_shared_migration(
    *, all_profiles: bool = False, profile: Optional[str] = None
) -> MigrationPlan:
    homes = _selected_profiles(all_profiles=all_profiles, profile=profile)
    target = (_root() / "auth.json").resolve(strict=False)
    gateway_homes = _gateway_homes_for_target(homes, target)
    locked_paths = sorted(
        {target, *(home / "auth.json" for home in homes), *(home / "config.yaml" for home in homes)},
        key=lambda path: os.fsencode(str(path.resolve(strict=False))),
    )
    with _auth_transition_lock(), _auth_store_locks(
        locked_paths, transaction_target=target
    ):
        gateway_preconditions = _gateway_snapshot(gateway_homes)
        manifest = _redacted_manifest(homes, target)
        plan_digest = _manifest_digest(manifest)
        preconditions = {
            str(target): _content_precondition(target),
            **{
                str(home / "auth.json"): _content_precondition(home / "auth.json")
                for home in homes
            },
            **{
                str(home / "config.yaml"): _content_precondition(home / "config.yaml")
                for home in homes
            },
        }
    plan_id = uuid.uuid4().hex
    artifact = _state_dir() / "plans" / f"{plan_id}.json"
    _private_json_write(
        artifact,
        {
            "version": 1,
            "plan_id": plan_id,
            "plan_digest": plan_digest,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "manifest": manifest,
            "target": str(target),
            "profile_homes": [str(home) for home in homes],
            "gateway_homes": [str(home) for home in gateway_homes],
            "gateway_preconditions": gateway_preconditions,
            "preconditions": preconditions,
        },
    )
    return MigrationPlan(plan_id, plan_digest, manifest, artifact)


def _load_plan(plan_id: str, plan_digest: str) -> dict[str, Any]:
    if not plan_id or Path(plan_id).name != plan_id:
        raise AuthMigrationError("A valid --plan-id is required")
    path = _state_dir() / "plans" / f"{plan_id}.json"
    if not path.is_file():
        raise AuthMigrationError("Migration plan was not found; run a new dry-run")
    plan = _read_json_object(path)
    if plan.get("plan_digest") != plan_digest:
        raise AuthMigrationError("Plan digest does not match the reviewed dry-run")
    if _manifest_digest(plan.get("manifest") or {}) != plan_digest:
        raise AuthMigrationError("Migration plan artifact failed integrity validation")
    return plan


def _merge_section(
    target: dict[str, Any], source: dict[str, Any], section: str, policy: str
) -> None:
    source_values = source.get(section)
    if not isinstance(source_values, dict):
        return
    target_values = target.setdefault(section, {})
    if not isinstance(target_values, dict):
        raise AuthMigrationError(f"Shared store {section} is not a mapping")
    for provider, value in source_values.items():
        if provider not in target_values:
            target_values[provider] = value
        elif target_values[provider] == value or policy == "prefer-shared":
            continue
        elif policy == "prefer-profile":
            target_values[provider] = value
        else:
            raise AuthMigrationError(
                f"Divergent {section} entry for provider {provider!r}; choose an explicit conflict policy"
            )


def _shared_authority_config(config_path: Path) -> tuple[dict[str, Any], bytes]:
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            config = fast_safe_load(handle) or {}
    else:
        config = {}
    if not isinstance(config, dict):
        raise AuthMigrationError(f"Config is not a mapping: {config_path}")
    auth = config.get("auth")
    if auth is None:
        auth = {}
    if not isinstance(auth, dict):
        raise AuthMigrationError(f"auth config is not a mapping: {config_path}")
    auth["authority"] = "shared"
    auth.pop("path", None)
    config["auth"] = auth
    stream = io.StringIO()
    yaml.dump(
        config,
        stream,
        Dumper=IndentDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return config, stream.getvalue().encode("utf-8")


def _set_shared_authority(
    config_path: Path, config: Optional[dict[str, Any]] = None
) -> None:
    if config is None:
        config, _ = _shared_authority_config(config_path)
    atomic_yaml_write(config_path, config)
    try:
        config_path.chmod(0o600)
    except OSError:
        pass


def apply_shared_migration(
    *,
    plan_id: str,
    plan_digest: str,
    conflict_policy: str,
    failure_injector: Optional[Callable[[str], None]] = None,
) -> str:
    if conflict_policy not in _POLICIES:
        raise AuthMigrationError(
            "--conflict-policy must be abort, prefer-shared, or prefer-profile"
        )
    plan = _load_plan(plan_id, plan_digest)
    target = Path(plan["target"])
    homes = [Path(item) for item in plan.get("profile_homes", [])]
    gateway_homes = [Path(item) for item in plan.get("gateway_homes", [])]
    paths = sorted(
        {target, *(home / "auth.json" for home in homes)},
        key=lambda path: os.fsencode(str(path.resolve(strict=False))),
    )
    journal_path = _state_dir() / "journals" / f"{plan_id}.json"
    backup_dir = _state_dir() / "backups" / plan_id
    journal: dict[str, Any] = {
        "version": 1,
        "plan_id": plan_id,
        "plan_digest": plan_digest,
        "phase": "planned",
        "target": str(target),
        "profile_homes": [str(home) for home in homes],
        "backup_dir": str(backup_dir),
        "preconditions": plan.get("preconditions", {}),
        "gateway_preconditions": plan.get("gateway_preconditions", {}),
    }
    _private_json_write(journal_path, journal)
    if failure_injector:
        failure_injector("planned")

    locked_paths = sorted(
        {*paths, *(home / "config.yaml" for home in homes)},
        key=lambda path: os.fsencode(str(path.resolve(strict=False))),
    )
    with _auth_transition_lock(), _auth_store_locks(
        locked_paths, transaction_target=target
    ):
        gateway_state = _gateway_snapshot(gateway_homes)
        running = next(
            ((home, pid) for home, pid in gateway_state.items() if pid is not None),
            None,
        )
        if running is not None:
            gateway_home, gateway_pid = running
            journal["phase"] = "aborted"
            journal["reason"] = "gateway_running"
            journal["gateway_home"] = gateway_home
            journal["gateway_pid"] = gateway_pid
            _private_json_write(journal_path, journal)
            raise AuthMigrationError(
                f"Relevant gateway PID {gateway_pid} is running for {gateway_home}; "
                "stop it and create a new migration plan"
            )
        if gateway_state != plan.get("gateway_preconditions", {}):
            journal["phase"] = "aborted"
            journal["reason"] = "gateway_process_state_changed"
            _private_json_write(journal_path, journal)
            raise AuthMigrationError(
                "Relevant gateway process state changed after dry-run; create a new plan"
            )
        journal["phase"] = "locked"
        _private_json_write(journal_path, journal)
        if failure_injector:
            failure_injector("locked")
        for raw_path, expected in plan.get("preconditions", {}).items():
            if _content_precondition(Path(raw_path)) != expected:
                journal["phase"] = "aborted"
                journal["reason"] = "precondition_changed"
                _private_json_write(journal_path, journal)
                raise AuthMigrationError(
                    "Migration inputs changed after dry-run; create a new plan"
                )

        backup_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copy2(target, backup_dir / "shared-auth.json")
            (backup_dir / "shared-auth.json").chmod(0o600)
        for home in homes:
            profile_dir = backup_dir / "profiles" / home.name
            profile_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(home / "auth.json", profile_dir / "auth.json")
            (profile_dir / "auth.json").chmod(0o600)
            if (home / "config.yaml").exists():
                shutil.copy2(home / "config.yaml", profile_dir / "config.yaml")
                (profile_dir / "config.yaml").chmod(0o600)
        journal["phase"] = "backed_up"
        journal["backup_preconditions"] = {
            str(path): _content_precondition(path)
            for path in backup_dir.rglob("*")
            if path.is_file()
        }
        _private_json_write(journal_path, journal)
        if failure_injector:
            failure_injector("backed_up")

        merged = _load_auth_store(target)
        for home in homes:
            source = _load_auth_store(home / "auth.json")
            _merge_section(merged, source, "providers", conflict_policy)
            _merge_section(merged, source, "credential_pool", conflict_policy)
        updated_at = datetime.now(timezone.utc).isoformat()
        merged["version"] = AUTH_STORE_VERSION
        merged["updated_at"] = updated_at
        target_expected = _raw_identity(
            (json.dumps(merged, indent=2) + "\n").encode("utf-8")
        )
        journal["phase"] = "target_write_pending"
        journal["target_written_postcondition"] = target_expected
        _private_json_write(journal_path, journal)
        if failure_injector:
            failure_injector("target_write_pending")
        _save_auth_store(merged, target_path=target, updated_at=updated_at)
        journal["phase"] = "target_written"
        _private_json_write(journal_path, journal)
        if failure_injector:
            failure_injector("target_written")

        journal["profile_config_postconditions"] = {}
        for home in homes:
            config_path = home / "config.yaml"
            _config, config_raw = _shared_authority_config(config_path)
            journal["phase"] = "profile_write_pending"
            journal["pending_profile_config"] = str(config_path)
            journal["profile_config_postconditions"][str(config_path)] = _raw_identity(
                config_raw
            )
            _private_json_write(journal_path, journal)
            if failure_injector:
                failure_injector("profile_write_pending")
            _set_shared_authority(config_path)
            if failure_injector:
                failure_injector("profile_written")
            journal.pop("pending_profile_config", None)
            _private_json_write(journal_path, journal)
        journal["phase"] = "profiles_configured"
        _private_json_write(journal_path, journal)
        if failure_injector:
            failure_injector("profiles_configured")
        journal["phase"] = "committed"
        journal["committed_at"] = datetime.now(timezone.utc).isoformat()
        journal["postcondition"] = _content_precondition(target)
        journal["config_postconditions"] = {
            str(home / "config.yaml"): _content_precondition(home / "config.yaml")
            for home in homes
        }
        _private_json_write(journal_path, journal)
    return plan_id


def recover_shared_migration(*, plan_id: str) -> str:
    """Roll back an incomplete migration from its private recovery journal."""
    if not plan_id or Path(plan_id).name != plan_id:
        raise AuthMigrationError("A valid --plan-id is required")
    journal_path = _state_dir() / "journals" / f"{plan_id}.json"
    if not journal_path.is_file():
        raise AuthMigrationError("Migration journal was not found")
    journal = _read_json_object(journal_path)
    phase = journal.get("phase")
    if phase in {"rolled_back", "aborted", "committed_state_changed"}:
        return str(phase)
    if phase == "manual_required":
        phase = journal.get("resume_phase")
        if not phase:
            raise AuthMigrationError(
                "Migration recovery requires manual intervention; preserved current state"
            )
    target = Path(journal["target"])
    homes = [Path(item) for item in journal.get("profile_homes", [])]
    backup_dir = Path(journal["backup_dir"])

    paths = sorted(
        {
            target,
            *(home / "auth.json" for home in homes),
            *(home / "config.yaml" for home in homes),
        },
        key=lambda path: os.fsencode(str(path.resolve(strict=False))),
    )
    with _auth_transition_lock(), _auth_store_locks(paths, transaction_target=target):

        def require_manual(reason: str) -> None:
            journal["phase"] = "manual_required"
            journal["resume_phase"] = phase
            journal["reason"] = reason
            journal["manual_required_at"] = datetime.now(timezone.utc).isoformat()
            _private_json_write(journal_path, journal)
            raise AuthMigrationError(
                "Migration state changed after interruption; refusing automatic recovery"
            )

        preconditions = journal.get("preconditions") or {}
        if phase == "committed":
            committed_ok = _matches_identity(target, journal.get("postcondition"))
            expected_configs = journal.get("config_postconditions") or {}
            committed_ok = committed_ok and all(
                _matches_identity(home / "config.yaml", expected_configs.get(str(home / "config.yaml")))
                for home in homes
            )
            if not committed_ok:
                journal["phase"] = "committed_state_changed"
                journal["reason"] = "committed_state_changed"
                journal["committed_state_changed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                _private_json_write(journal_path, journal)
                return "committed_state_changed"
            return "committed"

        if phase in {"planned", "locked"}:
            changed = any(
                not _matches_identity(Path(raw_path), expected)
                for raw_path, expected in preconditions.items()
            )
            journal["phase"] = "aborted"
            journal["reason"] = (
                "precondition_changed" if changed else "interrupted_before_mutation"
            )
            journal["aborted_at"] = datetime.now(timezone.utc).isoformat()
            _private_json_write(journal_path, journal)
            return "aborted"

        if phase == "backed_up":
            changed = any(
                not _matches_identity(Path(raw_path), expected)
                for raw_path, expected in preconditions.items()
            )
            journal["phase"] = "aborted"
            journal["reason"] = (
                "external_change_after_backup"
                if changed
                else "interrupted_before_mutation"
            )
            journal["aborted_at"] = datetime.now(timezone.utc).isoformat()
            _private_json_write(journal_path, journal)
            return "aborted"

        target_is_migration = _matches_identity(
            target, journal.get("target_written_postcondition")
        )
        target_is_original = _matches_identity(
            target, preconditions.get(str(target))
        )
        if phase == "target_write_pending":
            if not (target_is_migration or target_is_original):
                require_manual("external_change_after_backup")
        elif not target_is_migration:
            require_manual("committed_state_changed")

        expected_configs = journal.get("profile_config_postconditions") or {}
        migrated_configs: list[Path] = []
        for home in homes:
            config_path = home / "config.yaml"
            if _matches_identity(config_path, expected_configs.get(str(config_path))):
                migrated_configs.append(config_path)
            elif not _matches_identity(config_path, preconditions.get(str(config_path))):
                require_manual("committed_state_changed")

        for raw_path, expected in (journal.get("backup_preconditions") or {}).items():
            if not _matches_identity(Path(raw_path), expected):
                require_manual("backup_invalid")

        changed_by_migration = target_is_migration or bool(migrated_configs)
        if target_is_migration:
            target_backup = backup_dir / "shared-auth.json"
            if target_backup.is_file():
                _private_bytes_write(target, target_backup.read_bytes())
            else:
                target.unlink(missing_ok=True)
        for config in migrated_configs:
            home = config.parent
            config_backup = backup_dir / "profiles" / home.name / "config.yaml"
            if config_backup.is_file():
                _private_bytes_write(config, config_backup.read_bytes())
            else:
                config.unlink(missing_ok=True)
        journal["phase"] = "rolled_back" if changed_by_migration else "aborted"
        journal[f"{journal['phase']}_at"] = datetime.now(timezone.utc).isoformat()
        journal.pop("resume_phase", None)
        _private_json_write(journal_path, journal)
    return str(journal["phase"])


def rollback_shared_migration(*, plan_id: str) -> str:
    """Explicitly undo a committed migration when post-state is unchanged."""
    if not plan_id or Path(plan_id).name != plan_id:
        raise AuthMigrationError("A valid --plan-id is required")
    journal_path = _state_dir() / "journals" / f"{plan_id}.json"
    if not journal_path.is_file():
        raise AuthMigrationError("Migration journal was not found")
    journal = _read_json_object(journal_path)
    phase = journal.get("phase")
    if phase == "rolled_back":
        return "rolled_back"
    if phase != "committed":
        raise AuthMigrationError(
            "Only a committed migration can use --rollback; recover incomplete migrations instead"
        )

    target = Path(journal["target"])
    homes = [Path(item) for item in journal.get("profile_homes", [])]
    backup_dir = Path(journal["backup_dir"])
    paths = sorted(
        {
            target,
            *(home / "auth.json" for home in homes),
            *(home / "config.yaml" for home in homes),
        },
        key=lambda path: os.fsencode(str(path.resolve(strict=False))),
    )
    with _auth_transition_lock(), _auth_store_locks(paths, transaction_target=target):
        if _content_precondition(target) != journal.get("postcondition"):
            raise AuthMigrationError(
                "Committed shared auth changed after migration; refusing rollback"
            )
        expected_configs = journal.get("config_postconditions") or {}
        for home in homes:
            config_path = home / "config.yaml"
            if _content_precondition(config_path) != expected_configs.get(
                str(config_path)
            ):
                raise AuthMigrationError(
                    f"Profile config changed after migration: {home.name}; refusing rollback"
                )
        for raw_path, expected in (journal.get("backup_preconditions") or {}).items():
            if _content_precondition(Path(raw_path)) != expected:
                raise AuthMigrationError(
                    "Migration backup changed after commit; refusing rollback"
                )

        target_backup = backup_dir / "shared-auth.json"
        if target_backup.is_file():
            _private_bytes_write(target, target_backup.read_bytes())
        else:
            target.unlink(missing_ok=True)
        for home in homes:
            config = home / "config.yaml"
            config_backup = backup_dir / "profiles" / home.name / "config.yaml"
            if config_backup.is_file():
                _private_bytes_write(config, config_backup.read_bytes())
            else:
                config.unlink(missing_ok=True)
        journal["phase"] = "rolled_back"
        journal["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        journal["rollback_kind"] = "explicit_committed_rollback"
        _private_json_write(journal_path, journal)
    return "rolled_back"


def latest_migration_status() -> Optional[dict[str, Any]]:
    journals = _state_dir() / "journals"
    if not journals.exists():
        return None
    candidates = sorted(
        journals.glob("*.json"), key=lambda path: path.stat().st_mtime_ns
    )
    if not candidates:
        return None
    journal = _read_json_object(candidates[-1])
    return {
        "plan_id": journal.get("plan_id"),
        "phase": journal.get("phase"),
        "reason": journal.get("reason"),
    }
