"""Bounded explicit source checks for the existing LSP command."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from agent.lsp.manager import LSPService
from agent.lsp.outcome import operation_outcome, verification
from agent.lsp.servers import find_server_for_file

MAX_FILES = 16
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * MAX_FILE_BYTES
MAX_OUTPUT_CHARS = 20000
CHECK_TIMEOUT = 30.0


class CheckInputError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CheckInputError("deadline_exceeded")
    return remaining


def _git_identity(directory: Path, deadline: float) -> tuple[Path, str | None]:
    from hermes_cli._subprocess_compat import harden_git_argv, noninteractive_git_env
    env = noninteractive_git_env({k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    def invoke(args):
        try:
            return subprocess.run(["git", *harden_git_argv(args)], cwd=directory, env=env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=min(3.0, _remaining(deadline)))
        except subprocess.TimeoutExpired as exc:
            raise CheckInputError("deadline_exceeded") from exc
        except OSError as exc:
            raise CheckInputError("invalid_root") from exc
    result = invoke(["rev-parse", "--show-toplevel"])
    if result.returncode:
        raise CheckInputError("invalid_root")
    try:
        root = Path(result.stdout.removesuffix("\n")).resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise CheckInputError("invalid_root") from exc
    head = invoke(["rev-parse", "--verify", "HEAD"])
    commit = head.stdout.strip() if head.returncode == 0 else None
    if commit is not None and (len(commit) not in (40, 64) or any(c not in "0123456789abcdef" for c in commit)):
        raise CheckInputError("invalid_root")
    return root, commit


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _read_source(path: Path, deadline: float) -> dict:
    _remaining(deadline)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise CheckInputError("not_regular_file")
        if info.st_size > MAX_FILE_BYTES:
            raise CheckInputError("file_too_large")
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise CheckInputError("not_regular_file")
            data = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise CheckInputError("unreadable_file") from exc
    if len(data) > MAX_FILE_BYTES:
        raise CheckInputError("file_too_large")
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
    if identity(info) != identity(before) or identity(before) != identity(after):
        raise CheckInputError("source_changed")
    try:
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise CheckInputError("invalid_encoding") from exc
    _remaining(deadline)
    return {"text": text, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "identity": identity(after)}


def _preflight(paths: list[str], root: str | None, deadline: float):
    if not paths or len(paths) > MAX_FILES:
        raise CheckInputError("file_count_limit")
    try:
        directory = Path(root or os.getcwd()).expanduser().resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise CheckInputError("invalid_root") from exc
    context = _git_identity(directory, deadline)
    entries, seen, total, duplicates = [], {}, 0, 0
    for original in paths:
        _remaining(deadline)
        try:
            alias = Path(original).expanduser().absolute()
            path = alias.resolve(strict=False)
        except (OSError, ValueError, RuntimeError) as exc:
            raise CheckInputError("invalid_path") from exc
        if not _inside(path, context[0]):
            raise CheckInputError("outside_root")
        if path in seen:
            seen[path]["aliases"].append(alias)
            duplicates += 1
            continue
        entry = {"path": path, "aliases": [alias]}
        entries.append(entry)
        seen[path] = entry
        try:
            exists = path.exists()
        except OSError as exc:
            raise CheckInputError("unreadable_file") from exc
        if not exists:
            entry["reason"] = "missing_file"
            continue
        source = _read_source(path, deadline)
        if _git_identity(path.parent, deadline) != context:
            raise CheckInputError("outside_root")
        total += source["bytes"]
        if total > MAX_TOTAL_BYTES:
            raise CheckInputError("total_bytes_limit")
        entry["source"] = source
        if find_server_for_file(str(path)) is None:
            entry["reason"] = "unsupported_file"
    return context, entries, duplicates


def _stable(entry: dict, context: tuple, deadline: float) -> None:
    try:
        if any(alias.resolve(strict=True) != entry["path"] for alias in entry["aliases"]):
            raise CheckInputError("source_changed")
        current = _read_source(entry["path"], deadline)
    except (OSError, RuntimeError) as exc:
        raise CheckInputError("source_changed") from exc
    if current != entry["source"]:
        raise CheckInputError("source_changed")
    if _git_identity(entry["path"].parent, deadline) != context:
        raise CheckInputError("context_changed")


def _result(records, context, requested, duplicates, elapsed, cleanup, *, cancelled=False, omitted=0):
    payload = verification(records, omitted=omitted)
    payload.update(requested=requested, duplicates=duplicates, elapsed_seconds=round(elapsed, 6),
                   cleanup_seconds=round(cleanup, 6))
    if context is not None:
        root = operation_outcome(str(context[0]), "context", "context")
        payload["context"] = {"root": root["path"], "commit": context[1]}
        if "path_sha256" in root:
            payload["context"].update(root_sha256=root["path_sha256"], root_truncated=root["path_truncated"])
    payload["guidance"] = "For unchecked files, run the repository's documented type checks/tests. LSP does not replace tests."
    payload["exit_code"] = 130
    while len(json.dumps(payload, ensure_ascii=True)) > MAX_OUTPUT_CHARS and payload["files"]:
        payload["files"].pop()
        payload["omitted_files"] += 1
    incomplete = payload["omitted_files"] or any(row["status"] != "fresh" for row in records)
    code = 130 if cancelled else 2 if incomplete else 1 if any(row["total"]["count"] for row in records) else 0
    payload["exit_code"] = code
    return payload, code


def check_files(paths: list[str], *, root: str | None = None) -> tuple[dict, int]:
    """Check one explicit batch without source writes or managed dependency acquisition."""
    start = time.monotonic()
    deadline = start + CHECK_TIMEOUT
    records, context, duplicates, cancelled = [], None, 0, False
    try:
        context, entries, duplicates = _preflight(paths, root, deadline)
    except CheckInputError as exc:
        records = [operation_outcome(path, "check", exc.reason) for path in paths[:MAX_FILES]]
        if not records:
            records = [operation_outcome("", "check", exc.reason)]
        return _result(records, context, len(paths), 0, time.monotonic() - start, 0,
                       omitted=max(0, len(paths) - MAX_FILES))
    except KeyboardInterrupt:
        records = [operation_outcome(path, "check", "cancelled") for path in paths[:MAX_FILES]]
        return _result(records, None, len(paths), 0, time.monotonic() - start, 0, cancelled=True)
    service = None
    try:
        for index, entry in enumerate(entries):
            path = str(entry["path"])
            if "reason" in entry:
                records.append(operation_outcome(path, "check", entry["reason"]))
                continue
            if time.monotonic() >= deadline:
                records.append(operation_outcome(path, "check", "deadline_exceeded"))
                continue
            try:
                _stable(entry, context, deadline)
                if service is None:
                    try:
                        service = LSPService.create_from_config(install_strategy_override="manual")
                    except (TypeError, ValueError, OSError) as exc:
                        raise CheckInputError("invalid_config") from exc
                if service is None:
                    records.append(operation_outcome(path, "check", "service_unavailable", status="no_verdict"))
                    continue
                timeout = min(_remaining(deadline), service.get_status()["wait_timeout"] + 2.0)
                outcome = service.get_diagnostic_outcome_sync(path, delta=False, timeout=timeout,
                    post_content=entry["source"]["text"], read_only=True)
                _stable(entry, context, deadline)
                text, record = outcome.as_file(path, "check")
                if text:
                    record["lsp_diagnostics"] = text
                records.append(record)
            except CheckInputError as exc:
                records.append(operation_outcome(path, "check", exc.reason, status="no_verdict"))
            except KeyboardInterrupt:
                cancelled = True
                records.extend(operation_outcome(str(rest["path"]), "check", "cancelled") for rest in entries[index:])
                break
    finally:
        checked_at = time.monotonic()
        try:
            if service is not None:
                service.shutdown()
        except KeyboardInterrupt:
            cancelled = True
        cleanup = time.monotonic() - checked_at
    return _result(records, context, len(paths), duplicates, checked_at - start, cleanup, cancelled=cancelled)
