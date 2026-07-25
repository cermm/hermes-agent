"""Closed-world metadata for non-interactive Hermes query results.

This module deliberately projects the rich internal conversation result onto a
small, versioned schema.  It must never serialize responses, errors, prompts,
session identifiers, provider/model names, tool output, or traceback text.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from collections.abc import Mapping
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on native Windows
    fcntl = None  # type: ignore[assignment]

_SECURE_DIR_FD_AVAILABLE = all(
    call in os.supports_dir_fd for call in (os.open, os.stat, os.unlink, os.link)
)

SCHEMA_VERSION = "hermes-agent-result-meta-v1"
PUBLIC_ERROR_MESSAGE = "Error: failed to publish result metadata."
MAX_METADATA_BYTES = 1024
_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "completed",
        "failed",
        "partial",
        "interrupted",
        "api_calls",
        "failure_class",
    }
)
_FAILURE_CLASSES = frozenset(
    {
        "none",
        "interrupted",
        "content_policy_blocked",
        "provider_api_terminal",
        "max_turns_or_incomplete",
        "unknown_failure",
    }
)


class ResultMetadataError(RuntimeError):
    """The requested metadata cannot be projected or published safely."""


class ResultMetadataFD:
    """Single owner for a validated result-metadata FIFO write endpoint."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int) -> None:
        self._fd = fd

    @property
    def closed(self) -> bool:
        return self._fd < 0

    def fileno(self) -> int:
        if self.closed:
            raise ResultMetadataError("result metadata descriptor is closed")
        return self._fd

    def close(self) -> None:
        if self.closed:
            return
        fd = self._fd
        self._fd = -1
        try:
            os.close(fd)
        except OSError as exc:
            raise ResultMetadataError("result metadata descriptor close failed") from exc


def parse_result_metadata_fd(value: str) -> int:
    """Parse argparse input as a canonical decimal descriptor number."""

    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("result metadata descriptor must be a canonical integer")
    if value != str(int(value)):
        raise ValueError("result metadata descriptor must be a canonical integer")
    fd = int(value)
    if fd < 3:
        raise ValueError("result metadata descriptor must be at least 3")
    return fd


def _validate_result_metadata_fd(fd: Any) -> int:
    if os.name != "posix" or fcntl is None:
        raise ResultMetadataError("result metadata descriptor transport requires POSIX")
    if type(fd) is not int or fd < 3:
        raise ResultMetadataError("result metadata descriptor must be an integer at least 3")
    try:
        opened = os.fstat(fd)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except (OSError, TypeError, ValueError) as exc:
        raise ResultMetadataError("result metadata descriptor is invalid or closed") from exc
    if not stat.S_ISFIFO(opened.st_mode):
        raise ResultMetadataError("result metadata descriptor must be a FIFO")
    access_mode = flags & os.O_ACCMODE
    if access_mode != os.O_WRONLY:
        raise ResultMetadataError("result metadata descriptor must be a FIFO write endpoint")
    if flags & os.O_NONBLOCK:
        raise ResultMetadataError("result metadata descriptor must be blocking")
    try:
        pipe_buf = os.fpathconf(fd, "PC_PIPE_BUF")
    except (OSError, TypeError, ValueError) as exc:
        raise ResultMetadataError("result metadata FIFO atomic-write bound is unavailable") from exc
    if type(pipe_buf) is not int or pipe_buf < MAX_METADATA_BYTES:
        raise ResultMetadataError("result metadata FIFO atomic-write bound is too small")
    return fd


def claim_result_metadata_fd(fd: Any) -> ResultMetadataFD:
    """Validate and take ownership of a pre-opened result metadata descriptor.

    Validation does not write to the FIFO.
    The accepted descriptor is made non-inheritable before control returns.
    """

    validated_fd = _validate_result_metadata_fd(fd)
    try:
        os.set_inheritable(validated_fd, False)
    except OSError as exc:
        raise ResultMetadataError("result metadata descriptor could not be isolated") from exc
    return ResultMetadataFD(validated_fd)


def _require_secure_filesystem_primitives() -> None:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if os.name != "posix" or any(not hasattr(os, flag) for flag in required_flags):
        raise ResultMetadataError(
            "secure no-clobber result publication is unavailable on this platform"
        )
    if not _SECURE_DIR_FD_AVAILABLE:
        raise ResultMetadataError(
            "secure directory-relative result publication is unavailable on this platform"
        )


def _destination_parts(path: os.PathLike[str] | str) -> tuple[str, list[str]]:
    _require_secure_filesystem_primitives()
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise ResultMetadataError("result metadata destination must be a filesystem path") from exc
    if not isinstance(raw, str) or not raw.startswith("/") or raw.endswith("/") or "\x00" in raw:
        raise ResultMetadataError("result metadata destination must be an absolute file path")

    lexical_parts = raw.split("/")
    if ".." in lexical_parts:
        raise ResultMetadataError("result metadata destination must not contain '..'")
    components = [part for part in lexical_parts if part not in {"", "."}]
    if not components:
        raise ResultMetadataError("result metadata destination must name a file")
    return raw, components


def _open_parent_directory(path: os.PathLike[str] | str) -> tuple[int, str, str]:
    raw, components = _destination_parts(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    current_fd = -1
    try:
        current_fd = os.open("/", flags)
        for component in components[:-1]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
                raise ResultMetadataError("result metadata parent is not a directory")
        return current_fd, components[-1], raw
    except ResultMetadataError:
        if current_fd >= 0:
            os.close(current_fd)
        raise
    except OSError as exc:
        if current_fd >= 0:
            os.close(current_fd)
        raise ResultMetadataError("result metadata parent chain is unavailable or unsafe") from exc


def _leaf_exists(parent_fd: int, leaf: str) -> bool:
    try:
        os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def validate_result_metadata_destination(path: os.PathLike[str] | str) -> str:
    """Validate a safe, absent destination before model invocation.

    Publication repeats these checks using anchored directory descriptors,
    because preflight validation alone cannot close filesystem races.
    """

    parent_fd = -1
    try:
        parent_fd, leaf, raw = _open_parent_directory(path)
        if _leaf_exists(parent_fd, leaf):
            raise ResultMetadataError("result metadata destination already exists")
        return raw
    except ResultMetadataError:
        raise
    except OSError as exc:
        raise ResultMetadataError("result metadata destination is unavailable or unsafe") from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _parent_fd_still_names_destination(
    path: os.PathLike[str] | str,
    parent_fd: int,
) -> bool:
    """Check that the lexical parent still resolves to the anchored directory."""
    current_fd = -1
    try:
        current_fd, _leaf, _raw = _open_parent_directory(path)
        opened = os.fstat(parent_fd)
        current = os.fstat(current_fd)
        return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
    except (OSError, ResultMetadataError, ValueError):
        return False
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _api_call_count(result: Mapping[str, Any], max_iterations: int) -> tuple[int, bool]:
    if type(max_iterations) is not int or max_iterations < 0:
        max_iterations = 90
    upper_bound = max_iterations + 1  # The conversation loop permits one grace call.
    value = result.get("api_calls")
    if "api_calls" in result and type(value) is int and 0 <= value <= upper_bound:
        return value, True
    return 0, False


def _strict_statuses(result: Mapping[str, Any]) -> tuple[dict[str, bool], bool]:
    statuses: dict[str, bool] = {}
    valid = True
    for key in ("completed", "failed", "partial", "interrupted"):
        defaultable = key != "completed"
        value = result.get(key, False)
        if (key not in result and not defaultable) or type(value) is not bool:
            valid = False
            value = False
        statuses[key] = value
    return statuses, valid


def _is_max_turn_or_incomplete(result: Mapping[str, Any], statuses: Mapping[str, bool]) -> bool:
    if statuses["partial"] or not statuses["completed"]:
        return True
    exit_reason = result.get("turn_exit_reason")
    return isinstance(exit_reason, str) and (
        exit_reason.startswith("max_iterations_reached(")
        or exit_reason in {"budget_exhausted", "all_retries_exhausted_no_response"}
    )


def _is_trusted_provider_failure_reason(value: Any) -> bool:
    """Recognize only values emitted by the structured API error classifier."""
    if not isinstance(value, str):
        return False
    try:
        from agent.error_classifier import FailoverReason

        FailoverReason(value)
        return True
    except (ImportError, ValueError):
        return False


def _failure_class_invariant_error(
    failure_class: str,
    statuses: Mapping[str, bool],
) -> str | None:
    completed = statuses["completed"]
    failed = statuses["failed"]
    partial = statuses["partial"]
    interrupted = statuses["interrupted"]
    if failure_class == "none" and not (
        completed and not failed and not partial and not interrupted
    ):
        return "success metadata violates status invariants"
    if failure_class == "interrupted" and not interrupted:
        return "interrupted metadata violates status invariants"
    if failure_class in {"content_policy_blocked", "provider_api_terminal"} and not failed:
        return "terminal failure metadata violates status invariants"
    if failure_class == "max_turns_or_incomplete" and (
        completed or failed or interrupted
    ):
        return "incomplete metadata violates status invariants"
    return None


def build_result_metadata(result: Any, *, max_iterations: int) -> dict[str, Any]:
    """Project a trusted conversation result onto the public v1 metadata schema.

    ``api_calls`` is accepted only as a non-boolean integer in the inclusive
    range ``0..max_iterations + 1``.  Invalid or contradictory internal values
    are represented conservatively as ``unknown_failure``.
    """

    if not isinstance(result, Mapping):
        result = {}
        valid_shape = False
    else:
        valid_shape = True

    statuses, statuses_valid = _strict_statuses(result)
    api_calls, api_calls_valid = _api_call_count(result, max_iterations)
    valid = valid_shape and statuses_valid and api_calls_valid

    failure_class = "unknown_failure"

    if valid and sum(int(value) for value in statuses.values()) > 1:
        pass
    elif valid and statuses["interrupted"]:
        failure_class = "interrupted"
    elif valid and statuses["failed"] and isinstance(result.get("error"), str) and result[
        "error"
    ].startswith("content_policy_blocked:"):
        failure_class = "content_policy_blocked"
    elif valid and statuses["failed"] and _is_trusted_provider_failure_reason(
        result.get("failure_reason")
    ):
        failure_class = "provider_api_terminal"
    elif valid and statuses["failed"]:
        pass
    elif valid and _is_max_turn_or_incomplete(result, statuses):
        failure_class = "max_turns_or_incomplete"
    elif valid and statuses["completed"]:
        failure_class = "none"

    if _failure_class_invariant_error(failure_class, statuses) is not None:
        failure_class = "unknown_failure"

    return {
        "schema_version": SCHEMA_VERSION,
        "completed": statuses["completed"],
        "failed": statuses["failed"],
        "partial": statuses["partial"],
        "interrupted": statuses["interrupted"],
        "api_calls": api_calls,
        "failure_class": failure_class,
    }


def serialize_result_metadata(metadata: Mapping[str, Any]) -> bytes:
    """Return deterministic UTF-8 JSON with exactly the v1 public keys."""

    if set(metadata) != _RESULT_KEYS:
        raise ResultMetadataError("metadata does not match the closed public schema")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ResultMetadataError("unsupported metadata schema version")
    if metadata.get("failure_class") not in _FAILURE_CLASSES:
        raise ResultMetadataError("unsupported failure class")
    for key in ("completed", "failed", "partial", "interrupted"):
        if type(metadata.get(key)) is not bool:
            raise ResultMetadataError("status fields must be strict booleans")
    if type(metadata.get("api_calls")) is not int or metadata["api_calls"] < 0:
        raise ResultMetadataError("api_calls must be a non-negative integer")

    statuses = {
        key: metadata[key]
        for key in ("completed", "failed", "partial", "interrupted")
    }
    invariant_error = _failure_class_invariant_error(metadata["failure_class"], statuses)
    if invariant_error is not None:
        raise ResultMetadataError(invariant_error)

    payload = (
        json.dumps(
            dict(metadata),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_METADATA_BYTES:
        raise ResultMetadataError("result metadata exceeds the fixed size bound")
    return payload


def write_result_metadata_fd(
    owner: ResultMetadataFD,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Publish one bounded atomic frame through a validated FIFO writer."""

    if not isinstance(owner, ResultMetadataFD):
        raise ResultMetadataError("result metadata descriptor owner is invalid")
    payload = serialize_result_metadata(metadata)
    fd = owner.fileno()
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise ResultMetadataError("short write while publishing result metadata")
    except ResultMetadataError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise ResultMetadataError("could not write result metadata descriptor") from exc
    return dict(metadata)


def _same_inode(parent_fd: int, leaf: str, identity: tuple[int, int]) -> bool:
    try:
        current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (current.st_dev, current.st_ino) == identity


def _unlink_owned(
    parent_fd: int,
    leaf: str,
    identity: tuple[int, int],
    *,
    strict: bool,
) -> None:
    try:
        if not _same_inode(parent_fd, leaf, identity):
            if strict:
                raise ResultMetadataError("owned result metadata file was replaced")
            return
        os.unlink(leaf, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except ResultMetadataError:
        raise
    except OSError as exc:
        if strict:
            raise ResultMetadataError("could not remove result metadata staging file") from exc


def _retry_unlink_owned(parent_fd: int, leaf: str, identity: tuple[int, int]) -> None:
    # Never follow or remove a caller-swapped inode: every retry rechecks the
    # private staging file's identity with lstat semantics first.
    for _attempt in range(3):
        if not _same_inode(parent_fd, leaf, identity):
            return
        try:
            os.unlink(leaf, dir_fd=parent_fd)
            return
        except FileNotFoundError:
            return
        except OSError:
            continue


def _fsync_parent_directory(parent_fd: int) -> None:
    try:
        os.fsync(parent_fd)
    except OSError as exc:
        unsupported = {
            errno.EBADF,
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if exc.errno not in unsupported:
            raise


def write_result_metadata(
    path: os.PathLike[str] | str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically publish mode-0600 metadata without overwriting a leaf.

    The hard-link publication step provides atomic create-if-absent semantics.
    Platforms lacking anchored, no-follow directory operations fail closed.
    """

    payload = serialize_result_metadata(metadata)
    parent_fd = temp_fd = -1
    destination_name = ""
    temp_name: str | None = None
    temp_identity: tuple[int, int] | None = None
    destination_identity: tuple[int, int] | None = None
    published = False

    try:
        parent_fd, destination_name, _raw = _open_parent_directory(path)
        if _leaf_exists(parent_fd, destination_name):
            raise ResultMetadataError("result metadata destination already exists")

        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        create_flags |= getattr(os, "O_CLOEXEC", 0)
        for _attempt in range(16):
            candidate = f".{secrets.token_hex(16)}.result-meta.tmp"
            try:
                temp_fd = os.open(candidate, create_flags, 0o600, dir_fd=parent_fd)
                temp_name = candidate
                break
            except FileExistsError:
                continue
        if temp_fd < 0 or temp_name is None:
            raise ResultMetadataError("could not reserve result metadata staging file")

        os.fchmod(temp_fd, 0o600)
        opened_stat = os.fstat(temp_fd)
        temp_identity = (opened_stat.st_dev, opened_stat.st_ino)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ResultMetadataError("result metadata staging file is not regular")

        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(temp_fd, view[written:])
            if type(count) is not int or count <= 0:
                raise ResultMetadataError("short result metadata write made no progress")
            written += count
        os.fsync(temp_fd)

        # Link the already-open inode rather than its directory entry. This
        # prevents a caller with write access to the parent from swapping the
        # private temp name to a symlink between fsync and publication.
        fd_source = f"/proc/self/fd/{temp_fd}"
        try:
            source_stat = os.stat(fd_source)
        except OSError as exc:
            raise ResultMetadataError(
                "secure open-inode publication is unavailable on this platform"
            ) from exc
        if (source_stat.st_dev, source_stat.st_ino) != temp_identity:
            raise ResultMetadataError("result metadata staging identity changed")
        os.link(
            fd_source,
            destination_name,
            dst_dir_fd=parent_fd,
            follow_symlinks=True,
        )
        published = True
        destination_identity = temp_identity
        if not _same_inode(parent_fd, destination_name, temp_identity):
            raise ResultMetadataError("published result metadata identity changed")
        if not _parent_fd_still_names_destination(path, parent_fd):
            raise ResultMetadataError("destination parent changed during publication")

        _unlink_owned(parent_fd, temp_name, temp_identity, strict=True)
        temp_name = None
        os.close(temp_fd)
        temp_fd = -1
        _fsync_parent_directory(parent_fd)
        published = False  # Durable success: finally must not roll back the leaf.
        return dict(metadata)
    except ResultMetadataError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise ResultMetadataError("result metadata publication failed") from exc
    finally:
        if temp_fd >= 0:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if parent_fd >= 0:
            if published and destination_identity is not None:
                _retry_unlink_owned(parent_fd, destination_name, destination_identity)
            if temp_name is not None and temp_identity is not None:
                _retry_unlink_owned(parent_fd, temp_name, temp_identity)
            try:
                os.close(parent_fd)
            except OSError:
                pass
