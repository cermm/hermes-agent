#!/usr/bin/env python3
"""Reject unclassified production construction of a Hermes auth.json path."""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

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
_FLOW_OVERFLOW = "<too-many-static-alternatives>"
_MAX_FLOW_ALTERNATIVES = 128
_MAX_FORMAT_ATTEMPTS = 4096
_MAX_TEXT_FRAGMENT_CHAIN = 64
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


def _is_test_or_generated(relative: Path) -> bool:
    return any(part in _EXCLUDED_PARTS for part in relative.parts) or bool(
        _TEST_NAME_RE.search(relative.name)
    )


def _is_auth_store_reference(value: str) -> bool:
    # Fail closed when bounded flow analysis cannot retain every alternative.
    if _FLOW_OVERFLOW in value:
        return True
    normalized = value.replace("\\", "/").rstrip("/")
    return normalized == _AUTH_BASENAME or normalized.endswith(f"/{_AUTH_BASENAME}")


_BUILTIN_OPEN = "builtin_open"
_BUILTINS_MODULE = "builtins_module"
_PATH_CONSTRUCTOR = "path_constructor"
_PATHLIB_MODULE = "pathlib_module"
_DYNAMIC_PART = "<dynamic>"


@dataclass(frozen=True)
class _FlowValue:
    strings: frozenset[str] = frozenset()
    symbols: frozenset[str] = frozenset()

    def merged(self, other: "_FlowValue") -> "_FlowValue":
        return _FlowValue(
            strings=_bounded_strings(self.strings | other.strings),
            symbols=self.symbols | other.symbols,
        )


_UNKNOWN_VALUE = _FlowValue()


def _bounded_strings(values: Iterable[str]) -> frozenset[str]:
    retained: set[str] = set()
    for value in values:
        retained.add(value)
        if len(retained) > _MAX_FLOW_ALTERNATIVES:
            return frozenset([*sorted(retained)[:_MAX_FLOW_ALTERNATIVES], _FLOW_OVERFLOW])
    return frozenset(retained)


class _FlowScope:
    def __init__(
        self,
        parent: "_FlowScope | None" = None,
        *,
        local_names: set[str] | None = None,
        global_names: set[str] | None = None,
        nonlocal_names: set[str] | None = None,
        is_class_namespace: bool = False,
    ) -> None:
        self.parent = parent
        self.global_names = global_names or set()
        self.nonlocal_names = nonlocal_names or set()
        self.is_class_namespace = is_class_namespace
        self.bindings = {
            name: _UNKNOWN_VALUE
            for name in (local_names or set()) - self.global_names - self.nonlocal_names
        }

    def _root(self) -> "_FlowScope":
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def _nonlocal_target(self, name: str) -> "_FlowScope":
        scope = self.parent
        while scope is not None and scope.parent is not None:
            if name in scope.bindings:
                return scope
            scope = scope.parent
        return self.parent or self

    def _target(self, name: str) -> "_FlowScope":
        if name in self.global_names:
            return self._root()
        if name in self.nonlocal_names:
            return self._nonlocal_target(name)
        return self

    def assign(self, name: str, value: _FlowValue) -> None:
        self._target(name).bindings[name] = value

    def resolve(self, name: str) -> _FlowValue:
        if name in self.global_names:
            return self._root()._resolve_local_or_builtin(name)
        if name in self.nonlocal_names:
            return self._nonlocal_target(name)._resolve_local_or_parent(name)
        return self._resolve_local_or_parent(name)

    def _resolve_local_or_parent(self, name: str) -> _FlowValue:
        if name in self.bindings:
            return self.bindings[name]
        if self.parent is not None:
            return self.parent._resolve_local_or_parent(name)
        return self._builtin_value(name)

    def _resolve_local_or_builtin(self, name: str) -> _FlowValue:
        if name in self.bindings:
            return self.bindings[name]
        return self._builtin_value(name)

    @staticmethod
    def _builtin_value(name: str) -> _FlowValue:
        if name == "open":
            return _FlowValue(symbols=frozenset({_BUILTIN_OPEN}))
        if name in _PATH_CONSTRUCTORS:
            return _FlowValue(symbols=frozenset({_PATH_CONSTRUCTOR}))
        return _UNKNOWN_VALUE


class _ScopeDeclarations(ast.NodeVisitor):
    def __init__(self) -> None:
        self.local_names: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def _bind_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self.local_names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind_target(element)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind_target(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind_target(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._bind_target(node.target)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind_target(node.target)

    def visit_For(self, node: ast.For) -> None:
        self._bind_target(node.target)
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._bind_target(node.target)
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.local_names.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.local_names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.local_names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.local_names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.local_names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)


def _scope_declarations(statements: list[ast.stmt]) -> _ScopeDeclarations:
    declarations = _ScopeDeclarations()
    for statement in statements:
        declarations.visit(statement)
    return declarations


def _combine_strings(
    left: frozenset[str], right: frozenset[str], separator: str = ""
) -> frozenset[str]:
    left_values = left or frozenset({_DYNAMIC_PART})
    right_values = right or frozenset({_DYNAMIC_PART})
    return _bounded_strings(
        f"{first}{separator}{second}"
        for first in sorted(left_values)
        for second in sorted(right_values)
    )


def _join_string_parts(parts: list[frozenset[str]], separator: str) -> frozenset[str]:
    if not parts:
        return frozenset()
    combined = parts[0] or frozenset({_DYNAMIC_PART})
    for part in parts[1:]:
        combined = _combine_strings(combined, part, separator)
    return combined


class _PythonFlowAnalyzer:
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.findings: list[Finding] = []
        self.seen_lines: set[int] = set()
        self.deferred_functions: list[
            tuple[_FlowScope, ast.FunctionDef | ast.AsyncFunctionDef, _FlowScope]
        ] = []
        self.deferred_lambdas: list[tuple[_FlowScope, ast.Lambda, _FlowScope]] = []

    def analyze(self, tree: ast.Module) -> list[Finding]:
        scope = _FlowScope()
        self._analyze_block(tree.body, scope)
        self._flush_functions(scope)
        self._flush_lambdas(scope)
        return self.findings

    def _record(self, node: ast.AST, kind: str) -> None:
        line = getattr(node, "lineno", None)
        if line is None or line in self.seen_lines:
            return
        self.findings.append(Finding(self.relative, line, kind))
        self.seen_lines.add(line)

    def _expression_value(self, node: ast.AST, scope: _FlowScope) -> _FlowValue:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _FlowValue(strings=frozenset({node.value}))
        if isinstance(node, ast.Name):
            return scope.resolve(node.id)
        if isinstance(node, ast.NamedExpr):
            return self._expression_value(node.value, scope)
        if isinstance(node, ast.Attribute):
            owner = self._expression_value(node.value, scope)
            if node.attr == "open" and _BUILTINS_MODULE in owner.symbols:
                return _FlowValue(symbols=frozenset({_BUILTIN_OPEN}))
            if node.attr in _PATH_CONSTRUCTORS and _PATHLIB_MODULE in owner.symbols:
                return _FlowValue(symbols=frozenset({_PATH_CONSTRUCTOR}))
            return _UNKNOWN_VALUE
        if isinstance(node, ast.JoinedStr):
            parts = [
                self._expression_value(
                    value.value if isinstance(value, ast.FormattedValue) else value,
                    scope,
                ).strings
                for value in node.values
            ]
            return _FlowValue(strings=_join_string_parts(parts, ""))
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            return _FlowValue(
                strings=_combine_strings(
                    self._expression_value(node.left, scope).strings,
                    self._expression_value(node.right, scope).strings,
                    "/" if isinstance(node.op, ast.Div) else "",
                )
            )
        if not isinstance(node, ast.Call):
            return _UNKNOWN_VALUE

        callable_value = self._expression_value(node.func, scope)
        if _PATH_CONSTRUCTOR in callable_value.symbols and node.args:
            return _FlowValue(
                strings=_join_string_parts(
                    [self._expression_value(arg, scope).strings for arg in node.args],
                    "/",
                )
            )
        if not isinstance(node.func, ast.Attribute):
            return _UNKNOWN_VALUE

        method = node.func.attr
        owner_strings = self._expression_value(node.func.value, scope).strings
        if method == "with_suffix" and len(node.args) == 1:
            suffixes = self._expression_value(node.args[0], scope).strings
            suffix_values: list[str] = []
            for base in sorted(owner_strings):
                for suffix in sorted(suffixes):
                    try:
                        suffix_values.append(str(Path(base).with_suffix(suffix)))
                    except ValueError:
                        continue
            return _FlowValue(strings=_bounded_strings(suffix_values))
        if method == "joinpath":
            return _FlowValue(
                strings=_join_string_parts(
                    [owner_strings]
                    + [self._expression_value(arg, scope).strings for arg in node.args],
                    "/",
                )
            )
        if (
            method == "join"
            and len(node.args) == 1
            and isinstance(node.args[0], (ast.Tuple, ast.List))
        ):
            separators = owner_strings or frozenset({_DYNAMIC_PART})
            parts = [
                self._expression_value(element, scope).strings
                for element in node.args[0].elts
            ]
            joined_values = (
                value
                for separator in sorted(separators)
                for value in _join_string_parts(parts, separator)
            )
            return _FlowValue(strings=_bounded_strings(joined_values))
        if method == "join" and node.args:
            return _FlowValue(
                strings=_join_string_parts(
                    [self._expression_value(arg, scope).strings for arg in node.args],
                    "/",
                )
            )
        if method == "format" and owner_strings:
            positional_options = [
                self._expression_value(arg, scope).strings
                or frozenset({_DYNAMIC_PART})
                for arg in node.args
            ]
            keyword_items = [
                (
                    item.arg,
                    self._expression_value(item.value, scope).strings
                    or frozenset({_DYNAMIC_PART}),
                )
                for item in node.keywords
                if item.arg is not None
            ]
            values: set[str] = set()
            attempts = 0
            overflowed = False
            for template in sorted(owner_strings):
                positional_product = itertools.product(
                    *(sorted(options) for options in positional_options)
                )
                for positional in positional_product:
                    keyword_product = itertools.product(
                        *(sorted(options) for _, options in keyword_items)
                    )
                    for keyword_values in keyword_product:
                        attempts += 1
                        if attempts > _MAX_FORMAT_ATTEMPTS:
                            overflowed = True
                            break
                        keyword = {
                            name: value
                            for (name, _), value in zip(
                                keyword_items, keyword_values
                            )
                        }
                        try:
                            values.add(template.format(*positional, **keyword))
                        except (IndexError, KeyError, ValueError):
                            continue
                        if len(values) > _MAX_FLOW_ALTERNATIVES:
                            overflowed = True
                            break
                    if overflowed:
                        break
                if overflowed:
                    break
            if overflowed:
                values = set(sorted(values)[:_MAX_FLOW_ALTERNATIVES])
                values.add(_FLOW_OVERFLOW)
            return _FlowValue(strings=frozenset(values))
        return _UNKNOWN_VALUE

    def _finding_kind(self, node: ast.AST, scope: _FlowScope) -> Optional[str]:
        if isinstance(node, ast.Call):
            callable_value = self._expression_value(node.func, scope)
            if (
                _BUILTIN_OPEN in callable_value.symbols
                and node.args
                and any(
                    _is_auth_store_reference(value)
                    for value in self._expression_value(node.args[0], scope).strings
                )
            ):
                return "open"
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in _PATH_IO_METHODS
                and any(
                    _is_auth_store_reference(value)
                    for value in self._expression_value(node.func.value, scope).strings
                )
            ):
                return node.func.attr
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {
                    "join",
                    "joinpath",
                }
                and any(
                    _is_auth_store_reference(value)
                    for value in self._expression_value(node, scope).strings
                )
            ):
                return node.func.attr
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and any(
                _is_auth_store_reference(value)
                for value in self._expression_value(node, scope).strings
            )
        ):
            return "path_division"
        if isinstance(node, (ast.Call, ast.JoinedStr, ast.BinOp)) and any(
            _is_auth_store_reference(value)
            for value in self._expression_value(node, scope).strings
        ):
            return "constructed_path"
        return None

    def _scan_expression(self, node: ast.AST, scope: _FlowScope) -> None:
        if isinstance(node, ast.NamedExpr):
            self._scan_expression(node.value, scope)
            self._assign_target(
                node.target, self._expression_value(node.value, scope), scope
            )
            return
        if isinstance(node, ast.Lambda):
            self._analyze_lambda(node, scope)
            return
        if isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            self._analyze_comprehension(node, scope)
            return
        kind = self._finding_kind(node, scope)
        if kind is not None:
            self._record(node, kind)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._scan_expression(child, scope)

    @staticmethod
    def _target_names(target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return set().union(
                *(_PythonFlowAnalyzer._target_names(item) for item in target.elts)
            )
        return set()

    def _analyze_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        scope: _FlowScope,
    ) -> None:
        generators = node.generators
        if not generators:
            return
        self._scan_expression(generators[0].iter, scope)
        local_names = set().union(
            *(self._target_names(generator.target) for generator in generators)
        )
        child = _FlowScope(scope, local_names=local_names)
        for index, generator in enumerate(generators):
            if index:
                self._scan_expression(generator.iter, child)
            self._assign_target(generator.target, _UNKNOWN_VALUE, child)
            for condition in generator.ifs:
                self._scan_expression(condition, child)
        if isinstance(node, ast.DictComp):
            self._scan_expression(node.key, child)
            self._scan_expression(node.value, child)
        else:
            self._scan_expression(node.elt, child)

    def _analyze_lambda(self, node: ast.Lambda, scope: _FlowScope) -> None:
        defaults = [*node.args.defaults] + [
            item for item in node.args.kw_defaults if item is not None
        ]
        for default in defaults:
            self._scan_expression(default, scope)
        parameters = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        local_names = {item.arg for item in parameters}
        if node.args.vararg is not None:
            local_names.add(node.args.vararg.arg)
        if node.args.kwarg is not None:
            local_names.add(node.args.kwarg.arg)
        body_parent = scope
        while body_parent.is_class_namespace and body_parent.parent is not None:
            body_parent = body_parent.parent
        child = _FlowScope(body_parent, local_names=local_names)
        positional = [*node.args.posonlyargs, *node.args.args]
        if node.args.defaults:
            for argument, default in zip(
                positional[-len(node.args.defaults) :], node.args.defaults
            ):
                child.assign(argument.arg, self._expression_value(default, scope))
        for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is not None:
                child.assign(argument.arg, self._expression_value(default, scope))
        self.deferred_lambdas.append((body_parent, node, child))

    def _flush_lambdas(self, owner: _FlowScope) -> None:
        pending = [item for item in self.deferred_lambdas if item[0] is owner]
        self.deferred_lambdas = [
            item for item in self.deferred_lambdas if item[0] is not owner
        ]
        for _, node, child in pending:
            self._scan_expression(node.body, child)
            self._flush_lambdas(child)

    def _assign_target(
        self, target: ast.AST, value: _FlowValue, scope: _FlowScope
    ) -> None:
        if isinstance(target, ast.Name):
            scope.assign(target.id, value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._assign_target(element, _UNKNOWN_VALUE, scope)

    def _assign_expression_target(
        self, target: ast.AST, expression: ast.AST, scope: _FlowScope
    ) -> None:
        if (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(expression, (ast.Tuple, ast.List))
            and len(target.elts) == len(expression.elts)
        ):
            for target_item, value_item in zip(target.elts, expression.elts):
                self._assign_expression_target(target_item, value_item, scope)
            return
        self._assign_target(target, self._expression_value(expression, scope), scope)

    def _iterated_value(self, expression: ast.AST, scope: _FlowScope) -> _FlowValue:
        if not isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
            return _UNKNOWN_VALUE
        value = _UNKNOWN_VALUE
        for element in expression.elts:
            value = value.merged(self._expression_value(element, scope))
        return value

    def _scope_chain(self, scope: _FlowScope) -> list[_FlowScope]:
        chain = []
        current: Optional[_FlowScope] = scope
        while current is not None:
            chain.append(current)
            current = current.parent
        return list(reversed(chain))

    def _analyze_branches(
        self,
        scope: _FlowScope,
        branches: list[list[ast.stmt]],
        initial_bindings: list[dict[str, _FlowValue]] | None = None,
    ) -> None:
        chain = self._scope_chain(scope)
        original = [dict(item.bindings) for item in chain]
        outcomes: list[list[dict[str, _FlowValue]]] = []
        branch_bindings = initial_bindings or [{} for _ in branches]
        for branch, initial in zip(branches, branch_bindings):
            for item, saved in zip(chain, original):
                item.bindings = dict(saved)
            for name, value in initial.items():
                scope.assign(name, value)
            self._analyze_block(branch, scope)
            outcomes.append([dict(item.bindings) for item in chain])
        for index, item in enumerate(chain):
            names = set().union(*(outcome[index].keys() for outcome in outcomes))
            merged: dict[str, _FlowValue] = {}
            for name in names:
                value = _UNKNOWN_VALUE
                for outcome in outcomes:
                    value = value.merged(outcome[index].get(name, _UNKNOWN_VALUE))
                merged[name] = value
            item.bindings = merged

    def _function_scope(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, parent: _FlowScope
    ) -> _FlowScope:
        declarations = _scope_declarations(node.body)
        parameters = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        local_names = declarations.local_names | {item.arg for item in parameters}
        if node.args.vararg is not None:
            local_names.add(node.args.vararg.arg)
        if node.args.kwarg is not None:
            local_names.add(node.args.kwarg.arg)
        child = _FlowScope(
            parent,
            local_names=local_names,
            global_names=declarations.global_names,
            nonlocal_names=declarations.nonlocal_names,
        )
        positional = [*node.args.posonlyargs, *node.args.args]
        if node.args.defaults:
            default_parameters = positional[-len(node.args.defaults) :]
            for argument, default in zip(default_parameters, node.args.defaults):
                child.assign(argument.arg, self._expression_value(default, parent))
        for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is not None:
                child.assign(argument.arg, self._expression_value(default, parent))
        return child

    def _prepare_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, scope: _FlowScope
    ) -> _FlowScope:
        expressions = [*node.decorator_list, *node.args.defaults] + [
            item for item in node.args.kw_defaults if item is not None
        ]
        for expression in expressions:
            self._scan_expression(expression, scope)
        scope.assign(node.name, _UNKNOWN_VALUE)
        body_parent = scope
        while body_parent.is_class_namespace and body_parent.parent is not None:
            body_parent = body_parent.parent
        return self._function_scope(node, body_parent)

    def _analyze_function_body(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        child: _FlowScope,
        scope: _FlowScope,
    ) -> None:
        chain = self._scope_chain(scope)
        saved_bindings = [dict(item.bindings) for item in chain]
        self._analyze_block(node.body, child)
        self._flush_functions(child)
        self._flush_lambdas(child)
        for item, bindings in zip(chain, saved_bindings):
            item.bindings = bindings

    def _flush_functions(self, owner: _FlowScope) -> None:
        pending = [item for item in self.deferred_functions if item[0] is owner]
        self.deferred_functions = [
            item for item in self.deferred_functions if item[0] is not owner
        ]
        for _, node, child in pending:
            self._analyze_function_body(node, child, owner)

    def _analyze_statement(self, node: ast.stmt, scope: _FlowScope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            child = self._prepare_function(node, scope)
            self.deferred_functions.append((child.parent or scope, node, child))
            return
        if isinstance(node, ast.ClassDef):
            for expression in [*node.decorator_list, *node.bases]:
                self._scan_expression(expression, scope)
            scope.assign(node.name, _UNKNOWN_VALUE)
            declarations = _scope_declarations(node.body)
            child = _FlowScope(
                scope,
                global_names=declarations.global_names,
                nonlocal_names=declarations.nonlocal_names,
                is_class_namespace=True,
            )
            self._analyze_block(node.body, child)
            self._flush_functions(child)
            self._flush_lambdas(child)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if alias.name == "builtins":
                    value = _FlowValue(symbols=frozenset({_BUILTINS_MODULE}))
                elif alias.name == "pathlib":
                    value = _FlowValue(symbols=frozenset({_PATHLIB_MODULE}))
                else:
                    value = _UNKNOWN_VALUE
                scope.assign(name, value)
            return
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                if node.module == "builtins" and alias.name == "open":
                    value = _FlowValue(symbols=frozenset({_BUILTIN_OPEN}))
                elif node.module == "pathlib" and alias.name in _PATH_CONSTRUCTORS:
                    value = _FlowValue(symbols=frozenset({_PATH_CONSTRUCTOR}))
                else:
                    value = _UNKNOWN_VALUE
                scope.assign(name, value)
            return
        if isinstance(node, ast.Assign):
            self._scan_expression(node.value, scope)
            value = self._expression_value(node.value, scope)
            for target in node.targets:
                if isinstance(target, (ast.Tuple, ast.List)):
                    self._assign_expression_target(target, node.value, scope)
                else:
                    self._assign_target(target, value, scope)
            return
        if isinstance(node, ast.AnnAssign):
            value = _UNKNOWN_VALUE
            if node.value is not None:
                self._scan_expression(node.value, scope)
                value = self._expression_value(node.value, scope)
            self._assign_target(node.target, value, scope)
            return
        if isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            self._scan_expression(node.value, scope)
            self._assign_target(node.target, _UNKNOWN_VALUE, scope)
            return
        if isinstance(node, ast.If):
            self._scan_expression(node.test, scope)
            self._analyze_branches(scope, [node.body, node.orelse or []])
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._scan_expression(node.iter, scope)
            self._assign_target(node.target, self._iterated_value(node.iter, scope), scope)
            self._analyze_branches(scope, [node.body, []])
            self._analyze_block(node.orelse, scope)
            return
        if isinstance(node, ast.While):
            self._scan_expression(node.test, scope)
            self._analyze_branches(scope, [node.body, []])
            self._analyze_block(node.orelse, scope)
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                self._scan_expression(item.context_expr, scope)
                if item.optional_vars is not None:
                    self._assign_target(item.optional_vars, _UNKNOWN_VALUE, scope)
            self._analyze_block(node.body, scope)
            return
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if handler.type is not None:
                    self._scan_expression(handler.type, scope)
            handler_bindings = [
                {handler.name: _UNKNOWN_VALUE} if handler.name else {}
                for handler in node.handlers
            ]
            self._analyze_branches(
                scope,
                [node.body, *[handler.body for handler in node.handlers]],
                [{}, *handler_bindings],
            )
            self._analyze_block(node.orelse, scope)
            self._analyze_block(node.finalbody, scope)
            return
        if isinstance(
            node, (ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue)
        ):
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._scan_expression(child, scope)
            elif isinstance(child, ast.stmt):
                self._analyze_statement(child, scope)

    def _analyze_block(self, statements: list[ast.stmt], scope: _FlowScope) -> None:
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child = self._prepare_function(statement, scope)
                self.deferred_functions.append(
                    (child.parent or scope, statement, child)
                )
            else:
                self._analyze_statement(statement, scope)


def _python_findings(path: Path, relative: str) -> list[Finding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, UnicodeError, SyntaxError):
        return []
    return _PythonFlowAnalyzer(relative).analyze(tree)


_QUOTED_FRAGMENT_RE = re.compile(
    r"'([^'\\]*(?:\\.[^'\\]*)*)'|"
    r'"([^"\\]*(?:\\.[^"\\]*)*)"|'
    r"`([^`\\]*(?:\\.[^`\\]*)*)`"
)


def _strip_text_comments(source: str, suffix: str) -> str:
    output = list(source)
    quote: Optional[str] = None
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            else:
                output[index] = " "
            index += 1
            continue
        if block_comment:
            if char == "*" and following == "/":
                output[index] = output[index + 1] = " "
                block_comment = False
                index += 2
            else:
                if char != "\n":
                    output[index] = " "
                index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "/" and following == "*":
            output[index] = output[index + 1] = " "
            block_comment = True
            index += 2
            continue
        if char == "/" and following == "/" and suffix != ".sh":
            output[index] = output[index + 1] = " "
            line_comment = True
            index += 2
            continue
        if char == "#" and suffix in {".sh", ".nix"}:
            output[index] = " "
            line_comment = True
            index += 1
            continue
        index += 1
    return "".join(output)


def _fragment_value(match: re.Match[str]) -> str:
    return next(group for group in match.groups() if group is not None)


def _split_auth_store_reference_lines(source: str, suffix: str) -> set[int]:
    lines: set[int] = set()
    fragments = list(_QUOTED_FRAGMENT_RE.finditer(source))
    fragment_lines: list[int] = []
    line = 1
    cursor = 0
    for fragment in fragments:
        line += source.count("\n", cursor, fragment.start())
        fragment_lines.append(line)
        cursor = fragment.start()
    for start, first in enumerate(fragments):
        combined = _fragment_value(first)
        previous_end = first.end()
        following_fragments = itertools.islice(
            fragments,
            start + 1,
            start + 2 + _MAX_TEXT_FRAGMENT_CHAIN,
        )
        for offset, following in enumerate(following_fragments, start=1):
            separator = source[previous_end : following.start()]
            concatenates = separator == "" if suffix == ".sh" else bool(
                re.fullmatch(r"\s*\+\s*", separator)
            )
            if not concatenates:
                break
            if offset > _MAX_TEXT_FRAGMENT_CHAIN:
                # A longer static chain is pathological; flag it rather than
                # permitting chain length to become a scanner bypass.
                lines.add(fragment_lines[start])
                break
            combined += _fragment_value(following)
            if _is_auth_store_reference(combined):
                lines.add(fragment_lines[start])
                break
            previous_end = following.end()
    return lines


def _text_findings(path: Path, relative: str) -> list[Finding]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    source = _strip_text_comments(source, path.suffix.lower())
    split_reference_lines = _split_auth_store_reference_lines(
        source, path.suffix.lower()
    )
    return [
        Finding(relative, line_number, "text_reference")
        for line_number, line in enumerate(source.splitlines(), start=1)
        if _AUTH_BASENAME in line or line_number in split_reference_lines
    ]


def scan_repository(root: Path) -> list[Finding]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"scan root is not a directory: {root}")
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
    if not isinstance(payload, dict) or set(payload) != {"version", "consumers"}:
        raise ValueError(
            "auth-store consumer inventory must define exactly version and consumers"
        )
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
