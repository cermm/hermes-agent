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


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str


def _is_test_or_generated(relative: Path) -> bool:
    return any(part in _EXCLUDED_PARTS for part in relative.parts) or bool(
        _TEST_NAME_RE.search(relative.name)
    )


def _python_findings(path: Path, relative: str) -> list[Finding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, UnicodeError, SyntaxError):
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant)
            and node.right.value == _AUTH_BASENAME
        ):
            findings.append(Finding(relative, node.lineno, "path_division"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"join", "joinpath"} and any(
                isinstance(arg, ast.Constant) and arg.value == _AUTH_BASENAME
                for arg in node.args
            ):
                findings.append(Finding(relative, node.lineno, node.func.attr))
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


def load_inventory(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("consumers"), dict):
        raise ValueError("auth-store consumer inventory must use schema version 1")
    return {str(key): str(value) for key, value in payload["consumers"].items()}


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
