#!/usr/bin/env python3
"""Reject unclassified production construction of a Hermes auth.json path."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_TEXT_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".sh", ".nix"}
_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "tests",
    "test",
}
_TEST_NAME_RE = re.compile(r"(?:^test_|_test\.py$|\.test\.[^.]+$|\.spec\.[^.]+$)")
_AUTH_BASENAME = "auth.json"

# Reviewed exception contracts from the issue-380 consumer inventory. Reasons
# are machine values on purpose: free-form prose would let an inventory edit
# silence the rejector without selecting an approved behavior contract.
APPROVED_CLASSIFICATIONS = {
    "canonical_authority_owner": frozenset({"canonical_auth_authority"}),
    "whole_store_migration_adapter": frozenset({"canonical_locked_migration"}),
    "whole_store_backup_restore_adapter": frozenset(
        {"canonical_locked_backup_restore"}
    ),
    "profile_clone_export_adapter": frozenset({"explicit_profile_clone_export"}),
    "whole_store_boot_adapter": frozenset({"canonical_locked_bootstrap"}),
    "whole_store_deployment_adapter": frozenset(
        {"canonical_locked_deployment_seed"}
    ),
    "explicit_credential_copy_harness": frozenset(
        {"isolated_opt_in_credential_copy"}
    ),
    "provider_native_store": frozenset({"provider_native_non_hermes_store"}),
    "repository_integration_harness": frozenset(
        {"build_time_authority_contract_check"}
    ),
    "non_io_security_guard": frozenset({"non_io_guard_no_store_access"}),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str


@dataclass(frozen=True)
class InventoryEntry:
    category: str
    reason: str


def _is_test_or_generated(relative: Path) -> bool:
    return any(part in _EXCLUDED_PARTS for part in relative.parts) or bool(
        _TEST_NAME_RE.search(relative.name)
    )


def _is_auth_store_reference(value: str) -> bool:
    normalized = value.replace("\\", "/").rstrip("/")
    return normalized == _AUTH_BASENAME or normalized.endswith(f"/{_AUTH_BASENAME}")


def _static_string(node: ast.AST) -> Optional[str]:
    """Fold common static string/path forms without evaluating source code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                folded = _static_string(value.value)
                parts.append(folded if folded is not None else "<dynamic>")
            else:
                folded = _static_string(value)
                if folded is None:
                    return None
                parts.append(folded)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        if left is not None or right is not None:
            return (left if left is not None else "<dynamic>") + (
                right if right is not None else "<dynamic>"
            )
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id
            in {"Path", "PurePath", "PurePosixPath", "PureWindowsPath"}
            and node.args
        ):
            parts: list[str] = []
            for arg in node.args:
                folded = _static_string(arg)
                parts.append(folded if folded is not None else "<dynamic>")
            return "/".join(parts)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "with_suffix"
            and len(node.args) == 1
        ):
            base = _static_string(node.func.value)
            suffix = _static_string(node.args[0])
            if base is not None and suffix is not None:
                return str(Path(base).with_suffix(suffix))
    return None


def _python_findings(path: Path, relative: str) -> list[Finding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, UnicodeError, SyntaxError):
        return []
    findings: list[Finding] = []
    seen_lines: set[int] = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line is None:
            continue
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant)
            and node.right.value == _AUTH_BASENAME
        ):
            findings.append(Finding(relative, line, "path_division"))
            seen_lines.add(line)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"join", "joinpath"} and any(
                isinstance(arg, ast.Constant) and arg.value == _AUTH_BASENAME
                for arg in node.args
            ):
                findings.append(Finding(relative, line, node.func.attr))
                seen_lines.add(line)
        if line not in seen_lines and isinstance(
            node, (ast.Call, ast.JoinedStr, ast.BinOp)
        ):
            folded = _static_string(node)
            if folded is not None and _is_auth_store_reference(folded):
                findings.append(Finding(relative, line, "constructed_path"))
                seen_lines.add(line)
    return findings


def _text_findings(path: Path, relative: str) -> list[Finding]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    return [
        Finding(relative, line_number, "text_reference")
        for line_number, line in enumerate(lines, 1)
        if _AUTH_BASENAME in line
    ]


def scan_repository(root: Path) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root)
        if _is_test_or_generated(relative_path):
            continue
        relative = relative_path.as_posix()
        if path.suffix == ".py":
            findings.extend(_python_findings(path, relative))
        elif path.suffix in _TEXT_SUFFIXES:
            findings.extend(_text_findings(path, relative))
    return sorted(findings, key=lambda item: (item.path, item.line, item.kind))


def load_inventory(path: Path) -> dict[str, InventoryEntry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 2 or not isinstance(payload.get("consumers"), dict):
        raise ValueError("auth-store consumer inventory must use schema version 2")

    inventory: dict[str, InventoryEntry] = {}
    for raw_path, raw_entry in payload["consumers"].items():
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("auth-store consumer paths must be non-empty strings")
        candidate = Path(raw_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(
                f"auth-store consumer path must be repository-relative: {raw_path!r}"
            )
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"category", "reason"}:
            raise ValueError(
                f"auth-store consumer {raw_path!r} must define exactly category and reason"
            )
        category = raw_entry.get("category")
        reason = raw_entry.get("reason")
        if not isinstance(category, str) or category not in APPROVED_CLASSIFICATIONS:
            raise ValueError(
                f"auth-store consumer {raw_path!r} has unapproved category {category!r}"
            )
        if not isinstance(reason, str) or reason not in APPROVED_CLASSIFICATIONS[category]:
            raise ValueError(
                f"auth-store consumer {raw_path!r} has unapproved reason {reason!r} "
                f"for category {category!r}"
            )
        inventory[raw_path] = InventoryEntry(category=category, reason=reason)
    return inventory


def audit(root: Path, inventory_path: Path) -> tuple[list[Finding], list[str]]:
    findings = scan_repository(root)
    inventory = load_inventory(inventory_path)
    detected_paths = {finding.path for finding in findings}
    unclassified = [finding for finding in findings if finding.path not in inventory]
    stale = sorted(path for path in inventory if path not in detected_paths)
    return unclassified, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--inventory", type=Path)
    args = parser.parse_args(argv)
    inventory = args.inventory or args.root / "scripts" / "auth_store_consumer_inventory.json"
    try:
        unclassified, stale = audit(args.root, inventory)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"auth-store consumer audit failed: {exc}", file=sys.stderr)
        return 2
    if unclassified:
        print("Unclassified production auth.json consumers:", file=sys.stderr)
        for finding in unclassified:
            print(
                f"  {finding.path}:{finding.line}: {finding.kind}", file=sys.stderr
            )
        print(
            "Route access through the canonical authority API or add a reviewed "
            "adapter category to scripts/auth_store_consumer_inventory.json.",
            file=sys.stderr,
        )
    if stale:
        print("Stale auth.json consumer inventory entries:", file=sys.stderr)
        for path in stale:
            print(f"  {path}", file=sys.stderr)
    return 1 if unclassified or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
