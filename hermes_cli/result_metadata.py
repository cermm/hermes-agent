"""Closed-world metadata for non-interactive Hermes query results.

This module deliberately projects the rich internal conversation result onto a
small, versioned schema. It never serializes responses, errors, prompts,
session identifiers, provider/model names, tool output, or traceback text.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on native Windows
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = "hermes-agent-result-meta-v1"
PUBLIC_ERROR_MESSAGE = "Error: failed to publish result metadata."
MAX_METADATA_BYTES = 1024
MAX_API_CALLS = 32
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
_CLAIM_TOKEN = object()


class ResultMetadataError(RuntimeError):
    """The requested metadata cannot be projected or published safely."""


class ResultMetadataFD:
    """Single owner for a validated result-metadata FIFO write endpoint."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int, *, _claim_token: object | None = None) -> None:
        if _claim_token is not _CLAIM_TOKEN:
            raise ResultMetadataError(
                "result metadata descriptor owner must be claimed"
            )
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
            raise ResultMetadataError(
                "result metadata descriptor close failed"
            ) from exc


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
        raise ResultMetadataError(
            "result metadata descriptor transport requires POSIX"
        )
    if type(fd) is not int or fd < 3:
        raise ResultMetadataError(
            "result metadata descriptor must be an integer at least 3"
        )
    try:
        opened = os.fstat(fd)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise ResultMetadataError(
            "result metadata descriptor is invalid or closed"
        ) from exc
    if not stat.S_ISFIFO(opened.st_mode):
        raise ResultMetadataError("result metadata descriptor must be a FIFO")
    descriptor_target = None
    for descriptor_root in ("/proc/self/fd", "/dev/fd"):
        try:
            descriptor_target = os.readlink(f"{descriptor_root}/{fd}")
            break
        except OSError:
            continue
    if descriptor_target is None:
        raise ResultMetadataError(
            "result metadata descriptor identity is unavailable"
        )
    if not descriptor_target.startswith("pipe:"):
        raise ResultMetadataError(
            "result metadata descriptor must be an anonymous pipe"
        )
    if flags & os.O_ACCMODE != os.O_WRONLY:
        raise ResultMetadataError(
            "result metadata descriptor must be an anonymous-pipe write endpoint"
        )
    if flags & os.O_NONBLOCK:
        raise ResultMetadataError("result metadata descriptor must be blocking")
    try:
        pipe_buf = os.fpathconf(fd, "PC_PIPE_BUF")
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise ResultMetadataError(
            "result metadata pipe atomic-write bound is unavailable"
        ) from exc
    if type(pipe_buf) is not int or pipe_buf < MAX_METADATA_BYTES:
        raise ResultMetadataError(
            "result metadata pipe atomic-write bound is too small"
        )
    return fd


def _close_unclaimed_result_metadata_fd(fd: Any) -> None:
    """Best-effort cleanup for a descriptor rejected before ownership exists."""

    if type(fd) is not int or fd < 3:
        return
    try:
        os.close(fd)
    except (OSError, OverflowError):
        pass


def claim_result_metadata_fd(fd: Any) -> ResultMetadataFD:
    """Validate and take ownership of a pre-opened metadata descriptor."""

    try:
        validated_fd = _validate_result_metadata_fd(fd)
        os.set_inheritable(validated_fd, False)
    except ResultMetadataError:
        _close_unclaimed_result_metadata_fd(fd)
        raise
    except OSError as exc:
        _close_unclaimed_result_metadata_fd(fd)
        raise ResultMetadataError(
            "result metadata descriptor could not be isolated"
        ) from exc
    return ResultMetadataFD(validated_fd, _claim_token=_CLAIM_TOKEN)


def _api_call_count(
    result: Mapping[str, Any], max_iterations: int
) -> tuple[int, bool]:
    if type(max_iterations) is not int or max_iterations < 0:
        max_iterations = 90
    upper_bound = min(max_iterations + 1, MAX_API_CALLS)
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


def _is_max_turn_or_incomplete(
    result: Mapping[str, Any], statuses: Mapping[str, bool]
) -> bool:
    if statuses["partial"] or not statuses["completed"]:
        return True
    exit_reason = result.get("turn_exit_reason")
    return isinstance(exit_reason, str) and (
        exit_reason.startswith("max_iterations_reached(")
        or exit_reason
        in {"budget_exhausted", "all_retries_exhausted_no_response"}
    )


def _is_trusted_provider_failure_reason(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        from agent.error_classifier import FailoverReason

        FailoverReason(value)
        return True
    except (ImportError, ValueError):
        return False


def _failure_class_invariant_error(
    failure_class: str, statuses: Mapping[str, bool]
) -> str | None:
    completed = statuses["completed"]
    failed = statuses["failed"]
    partial = statuses["partial"]
    interrupted = statuses["interrupted"]
    multi_status = sum(int(value) for value in statuses.values()) > 1
    if failure_class != "unknown_failure" and multi_status:
        return "non-unknown metadata violates status exclusivity invariants"
    if failure_class == "none" and not (
        completed and not failed and not partial and not interrupted
    ):
        return "success metadata violates status invariants"
    if failure_class == "interrupted" and (
        not interrupted or completed or failed or partial
    ):
        return "interrupted metadata violates status invariants"
    if failure_class in {"content_policy_blocked", "provider_api_terminal"} and not failed:
        return "terminal failure metadata violates status invariants"
    if failure_class == "max_turns_or_incomplete" and not (
        partial and not completed and not failed and not interrupted
    ):
        return "incomplete metadata violates status invariants"
    return None


def build_result_metadata(
    result: Any, *, max_iterations: int
) -> dict[str, Any]:
    """Project a trusted turn result onto the public v1 metadata schema."""

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
    elif (
        valid
        and statuses["failed"]
        and isinstance(result.get("error"), str)
        and result["error"].startswith("content_policy_blocked:")
    ):
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
    if (
        type(metadata.get("api_calls")) is not int
        or not 0 <= metadata["api_calls"] <= MAX_API_CALLS
    ):
        raise ResultMetadataError("api_calls must be a bounded non-negative integer")

    statuses = {
        key: metadata[key]
        for key in ("completed", "failed", "partial", "interrupted")
    }
    invariant_error = _failure_class_invariant_error(
        metadata["failure_class"], statuses
    )
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
    owner: ResultMetadataFD, metadata: Mapping[str, Any]
) -> dict[str, Any]:
    """Publish one bounded atomic frame through a validated FIFO writer."""

    if not isinstance(owner, ResultMetadataFD):
        raise ResultMetadataError("result metadata descriptor owner is invalid")
    payload = serialize_result_metadata(metadata)
    fd = owner.fileno()
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise ResultMetadataError(
                "short write while publishing result metadata"
            )
    except ResultMetadataError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise ResultMetadataError(
            "could not write result metadata descriptor"
        ) from exc
    return dict(metadata)


def publish_unknown_failure_result_metadata_fd(
    owner: ResultMetadataFD,
) -> dict[str, Any]:
    """Publish and close a safe terminal frame for a pre-turn failure."""

    publication_error: ResultMetadataError | None = None
    metadata = build_result_metadata(
        {
            "completed": False,
            "failed": True,
            "partial": False,
            "interrupted": False,
            "api_calls": 0,
        },
        max_iterations=0,
    )
    try:
        write_result_metadata_fd(owner, metadata)
    except ResultMetadataError as exc:
        publication_error = exc
    try:
        owner.close()
    except ResultMetadataError as exc:
        if publication_error is None:
            publication_error = exc
    if publication_error is not None:
        raise ResultMetadataError(
            "failed to publish terminal result metadata"
        ) from publication_error
    return metadata
