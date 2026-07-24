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
_PATH_IO_METHODS = {"open", "read_bytes", "read_text", "write_bytes", "write_text"}
_PATH_CONSTRUCTORS = {"Path", "PurePath", "PurePosixPath", "PureWindowsPath"}

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


@dataclass(frozen=True)
class _PythonAliases:
    open_names: frozenset[str]
    builtins_modules: frozenset[str]
    path_constructors: frozenset[str]
    pathlib_modules: frozenset[str]


def _is_test_or_generated(relative: Path) -> bool:
    return any(part in _EXCLUDED_PARTS for part in relative.parts) or bool(
        _TEST_NAME_RE.search(relative.name)
    )


def _is_auth_store_reference(value: str) -> bool:
    normalized = value.replace("\\", "/").rstrip("/")
    return normalized == _AUTH_BASENAME or normalized.endswith(f"/{_AUTH_BASENAME}")


def _python_aliases(tree: ast.Module) -> _PythonAliases:
    open_names = {"open"}
    builtins_modules: set[str] = set()
    path_constructors = set(_PATH_CONSTRUCTORS)
    pathlib_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name
                if alias.name == "builtins":
                    builtins_modules.add(bound_name)
                elif alias.name == "pathlib":
                    pathlib_modules.add(bound_name)
        elif isinstance(node, ast.ImportFrom) and node.module == "builtins":
            for alias in node.names:
                if alias.name == "open":
                    open_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "pathlib":
            for alias in node.names:
                if alias.name in _PATH_CONSTRUCTORS:
                    path_constructors.add(alias.asname or alias.name)
    return _PythonAliases(
        open_names=frozenset(open_names),
        builtins_modules=frozenset(builtins_modules),
        path_constructors=frozenset(path_constructors),
        pathlib_modules=frozenset(pathlib_modules),
    )


def _is_path_constructor(node: ast.AST, aliases: _PythonAliases) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases.path_constructors
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _PATH_CONSTRUCTORS
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases.pathlib_modules
    )


def _is_builtin_open(node: ast.AST, aliases: _PythonAliases) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases.open_names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "open"
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases.builtins_modules
    )


def _static_string(
    node: ast.AST,
    bindings: Optional[dict[str, str]] = None,
    aliases: Optional[_PythonAliases] = None,
) -> Optional[str]:
    """Fold common static string/path forms without evaluating source code."""
    aliases = aliases or _PythonAliases(
        open_names=frozenset({"open"}),
        builtins_modules=frozenset(),
        path_constructors=frozenset(_PATH_CONSTRUCTORS),
        pathlib_modules=frozenset(),
    )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and bindings is not None:
        return bindings.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                folded = _static_string(value.value, bindings, aliases)
                parts.append(folded if folded is not None else "<dynamic>")
            else:
                folded = _static_string(value, bindings, aliases)
                if folded is None:
                    return None
                parts.append(folded)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left, bindings, aliases)
        right = _static_string(node.right, bindings, aliases)
        if left is not None or right is not None:
            return (left if left is not None else "<dynamic>") + (
                right if right is not None else "<dynamic>"
            )
    if isinstance(node, ast.Call):
        if _is_path_constructor(node.func, aliases) and node.args:
            parts: list[str] = []
            for arg in node.args:
                folded = _static_string(arg, bindings, aliases)
                parts.append(folded if folded is not None else "<dynamic>")
            return "/".join(parts)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "with_suffix"
            and len(node.args) == 1
        ):
            base = _static_string(node.func.value, bindings, aliases)
            suffix = _static_string(node.args[0], bindings, aliases)
            if base is not None and suffix is not None:
                return str(Path(base).with_suffix(suffix))
    return None


class _ScopeAssignmentCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.assignments: dict[str, list[ast.AST]] = {}
        self.invalidated: set[str] = set()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.assignments.setdefault(target.id, []).append(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self.assignments.setdefault(node.target.id, []).append(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.invalidated.add(node.target.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return


def _scope_static_string_bindings(
    statements: list[ast.stmt],
    aliases: _PythonAliases,
    inherited: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Resolve exactly-once static string assignments in one lexical scope."""
    collector = _ScopeAssignmentCollector()
    for statement in statements:
        collector.visit(statement)

    assignments = collector.assignments
    invalidated = collector.invalidated
    bindings = dict(inherited or {})
    for name in assignments.keys() | invalidated:
        bindings.pop(name, None)
    unresolved = {
        name: values[0]
        for name, values in collector.assignments.items()
        if len(values) == 1 and name not in invalidated
    }
    while unresolved:
        resolved_names: list[str] = []
        for name, value in unresolved.items():
            folded = _static_string(value, bindings, aliases)
            if folded is not None and "<dynamic>" not in folded:
                bindings[name] = folded
                resolved_names.append(name)
        if not resolved_names:
            break
        for name in resolved_names:
            del unresolved[name]
    return bindings


def _bindings_by_node(
    tree: ast.Module, aliases: _PythonAliases
) -> dict[int, dict[str, str]]:
    module_bindings = _scope_static_string_bindings(tree.body, aliases)
    bindings_by_node: dict[int, dict[str, str]] = {}

    class BindingMapper(ast.NodeVisitor):
        def __init__(self) -> None:
            self.bindings = module_bindings

        def visit(self, node: ast.AST):
            bindings_by_node[id(node)] = self.bindings
            return super().visit(node)

        def _visit_function(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef
        ) -> None:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self.visit(default)
            previous = self.bindings
            self.bindings = _scope_static_string_bindings(
                node.body, aliases, inherited=previous
            )
            argument_names = {
                argument.arg
                for argument in [
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                ]
            }
            if node.args.vararg is not None:
                argument_names.add(node.args.vararg.arg)
            if node.args.kwarg is not None:
                argument_names.add(node.args.kwarg.arg)
            for name in argument_names:
                self.bindings.pop(name, None)
            for statement in node.body:
                self.visit(statement)
            self.bindings = previous

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

    BindingMapper().visit(tree)
    return bindings_by_node


def _python_findings(path: Path, relative: str) -> list[Finding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, UnicodeError, SyntaxError):
        return []
    aliases = _python_aliases(tree)
    bindings_by_node = _bindings_by_node(tree, aliases)
    findings: list[Finding] = []
    seen_lines: set[int] = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line is None:
            continue
        bindings = bindings_by_node.get(id(node), {})
        if (
            isinstance(node, ast.Call)
            and _is_builtin_open(node.func, aliases)
            and node.args
            and (target := _static_string(node.args[0], bindings, aliases)) is not None
            and _is_auth_store_reference(target)
        ):
            findings.append(Finding(relative, line, "open"))
            seen_lines.add(line)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _PATH_IO_METHODS
            and (target := _static_string(node.func.value, bindings, aliases)) is not None
            and _is_auth_store_reference(target)
        ):
            findings.append(Finding(relative, line, node.func.attr))
            seen_lines.add(line)
        elif (
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
            folded = _static_string(node, bindings, aliases)
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
