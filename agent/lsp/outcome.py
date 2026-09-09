"""One diagnostic snapshot and bounded, model-visible file verification evidence."""
from __future__ import annotations

import hashlib
from concurrent.futures import CancelledError
import json
from dataclasses import dataclass, field
from typing import Any
from agent.lsp import reporter

MAX_OUTCOME_FILES = 32
MAX_OUTCOME_CHARS = 16000


def failure_reason(exc: Exception) -> str:
    if isinstance(exc, CancelledError):
        return "cancelled"
    return "timeout" if isinstance(exc, TimeoutError) else "server_error"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def normalized_text(text: str) -> str:
    # Matches Path.read_text's universal-newline and replacement decoding.
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def counts(diags: list[dict]) -> dict:
    result = dict.fromkeys(("count", "error", "warning", "information", "hint", "unknown"), 0)
    names = {1: "error", 2: "warning", 3: "information", 4: "hint"}
    for diag in diags:
        severity = diag.get("severity", 1)
        if severity is None:
            severity = 1
        name = names.get(severity, "unknown") if type(severity) is int else "unknown"
        result[name] += 1
        result["count"] += 1
    return result


@dataclass
class DiagnosticOutcome:
    status: str
    reason: str
    diagnostics: list[dict] = field(default_factory=list)
    delta: list[dict] | None = None
    baseline: str = "not_requested"
    baseline_reason: str | None = None
    server_id: str | None = None
    document_version: int | None = None
    text: str | None = None

    def as_file(self, path: str, operation: str = "write") -> tuple[str, dict]:
        record = operation_outcome(path, operation, self.reason, status=self.status)
        record.update(baseline=self.baseline, total=counts(self.diagnostics) if self.status == "fresh" else None,
                      delta=counts(self.delta) if self.delta is not None else None)
        if self.baseline_reason:
            record["baseline_reason"] = self.baseline_reason
        if self.status == "fresh" and self.text is not None:
            record["source"] = {
                "server_id": reporter._sanitize_field(self.server_id, limit=80),
                "document_version": self.document_version, "text_sha256": text_hash(self.text),
                "freshness": "client_tracked"}
        selected = self.delta if self.delta is not None else self.diagnostics
        text, report = render_diagnostics(path, selected, baseline_known=self.baseline == "available",
                                          baseline_requested=self.baseline != "not_requested")
        record["report"] = report
        return text, record


def operation_outcome(path: str, operation: str, reason: str, *, status: str = "not_checked") -> dict:
    safe = reporter._sanitize_field(path, limit=512)
    record = {"path": safe, "operation": operation, "status": status, "reason": reason,
              "baseline": "not_requested", "total": None, "delta": None}
    if len(str(path)) > 512 or safe != str(path):
        record.update(path_truncated=len(str(path)) > 512, path_sha256=text_hash(str(path)))
    return record


def verification(records: list[dict], *, omitted: int = 0) -> dict:
    result = {"files": [], "omitted_files": omitted + len(records)}
    for record in records[:MAX_OUTCOME_FILES]:
        result["files"].append(record)
        result["omitted_files"] -= 1
        if len(json.dumps(result, ensure_ascii=True)) > MAX_OUTCOME_CHARS:
            result["files"].pop()
            result["omitted_files"] += 1
            break
    return result


def render_diagnostics(path: str, diagnostics: list[dict], *, baseline_known: bool,
                       baseline_requested: bool = True) -> tuple[str, dict]:
    eligible = [d for d in diagnostics if type(d.get("severity") or 1) is int
                and (d.get("severity") or 1) in reporter.DEFAULT_SEVERITIES]
    meta = {"eligible": len(eligible), "rendered": 0, "truncated": False}
    if not eligible:
        return "", meta
    prefix = ("LSP diagnostics introduced by this edit:\n" if baseline_known else
              "Current LSP diagnostics (baseline unavailable):\n" if baseline_requested else
              "Current LSP diagnostics:\n")
    safe_path = reporter._sanitize_field(path, limit=512).replace('"', "&quot;")
    header, footer = f'<diagnostics file="{safe_path}">\n', "\n</diagnostics>"
    lines = []
    for diag in eligible[:reporter.MAX_PER_FILE]:
        try:
            line = reporter.format_diagnostic(diag)
        except (TypeError, ValueError, OverflowError, AttributeError):
            meta["truncated"] = True
            continue
        candidate = prefix + header + "\n".join([*lines, line]) + footer
        if len(candidate) > reporter.MAX_TOTAL_CHARS:
            break
        lines.append(line)
    meta["rendered"] = len(lines)
    meta["truncated"] |= len(lines) < len(eligible)
    return (prefix + header + "\n".join(lines) + footer if lines else ""), meta


def ensure_verification(result: dict, paths: list[str], operation: str) -> dict:
    """Old file-provider payloads contain no evidence; do not infer a semantic verdict."""
    if "lsp_verification" not in result:
        reason = "write_failed" if result.get("error") else "no_change" if result.get("no_change") else "legacy_provider"
        result["lsp_verification"] = verification([operation_outcome(p, operation, reason) for p in paths])
    return result


def unchecked_verification(paths: list[str], operation: str, reason: str) -> dict:
    """Bounded evidence for an operation rejected before a semantic query."""
    return verification([operation_outcome(p, operation, reason) for p in paths if p])
