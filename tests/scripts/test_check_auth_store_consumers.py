from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_auth_store_consumers.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_auth_store_consumers", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _entry(category: str, reason: str) -> dict[str, str]:
    return {"category": category, "reason": reason}


def _inventory(path: Path, consumers: dict[str, dict[str, str]]) -> Path:
    inventory = path / "inventory.json"
    inventory.write_text(
        json.dumps({"version": 2, "consumers": consumers}), encoding="utf-8"
    )
    return inventory


def test_audit_rejects_unclassified_python_path_construction(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "new_consumer.py"
    source.write_text(
        'from pathlib import Path\nAUTH = Path.home() / ".hermes" / "auth.json"\n',
        encoding="utf-8",
    )

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("new_consumer.py", 2, "path_division")
    ]
    assert stale == []


def test_audit_accepts_reviewed_category_and_rejects_stale_entries(
    tmp_path: Path,
) -> None:
    module = _load_module()
    source = tmp_path / "adapter.py"
    source.write_text(
        'from pathlib import Path\nAUTH = Path("root").joinpath("auth.json")\n',
        encoding="utf-8",
    )
    inventory = _inventory(
        tmp_path,
        {
            "adapter.py": _entry(
                "whole_store_deployment_adapter",
                "canonical_locked_deployment_seed",
            ),
            "removed.py": _entry(
                "canonical_authority_owner", "canonical_auth_authority"
            ),
        },
    )

    unclassified, stale = module.audit(tmp_path, inventory)

    assert unclassified == []
    assert stale == ["removed.py"]


def test_scan_ignores_tests_but_covers_non_python_deployment_adapters(
    tmp_path: Path,
) -> None:
    module = _load_module()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fixture.py").write_text(
        'AUTH = Path("root") / "auth.json"\n', encoding="utf-8"
    )
    hook = tmp_path / "stage2-hook.sh"
    hook.write_text('target="$HERMES_HOME/auth.json"\n', encoding="utf-8")

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("stage2-hook.sh", 1, "text_reference")
    ]


def test_scan_normalizes_relative_and_absolute_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(
        'open("auth.json", "rb")\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    relative_findings = module.scan_repository(Path("."))
    absolute_findings = module.scan_repository(tmp_path.resolve())

    assert relative_findings == absolute_findings == [
        module.Finding("consumer.py", 1, "open")
    ]


@pytest.mark.parametrize(
    "source",
    [
        'AUTH_STORE = Path("auth.json")\n',
        'AUTH_STORE = Path(f"{home}/auth.json")\n',
        'AUTH_STORE = Path("auth" + ".json")\n',
        'AUTH_STORE = str(home) + "/auth.json"\n',
        'AUTH_STORE = Path(home, "auth.json")\n',
        'AUTH_STORE = Path("auth").with_suffix(".json")\n',
    ],
)
def test_audit_rejects_constructed_auth_store_paths(
    tmp_path: Path, source: str
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(
        "from pathlib import Path\n" + source, encoding="utf-8"
    )

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line) for item in unclassified] == [("consumer.py", 2)]
    assert stale == []


def test_audit_rejects_direct_builtin_open_of_auth_store(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(
        'open("auth.json", "rb")\n', encoding="utf-8"
    )

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", 1, "open")
    ]
    assert stale == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        ('open(file="auth.json", mode="rb")\n', 1),
        ('import builtins\nbuiltins.open(file="auth.json", mode="rb")\n', 2),
    ],
)
def test_audit_rejects_builtin_open_file_keyword(
    tmp_path: Path, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", expected_line, "open")
    ]
    assert stale == []


def test_audit_rejects_direct_wrapper_receiving_static_auth_store(
    tmp_path: Path,
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(
        "def consume(path):\n"
        '    return open(path, "r").read()\n'
        'consume("auth.json")\n',
        encoding="utf-8",
    )

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", 2, "open")
    ]
    assert stale == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        pytest.param(
            'open(*("auth.json", "rb"))\n',
            1,
            id="builtin-open-star-args",
        ),
        pytest.param(
            'open(**{"file": "auth.json", "mode": "rb"})\n',
            1,
            id="builtin-open-star-star-kwargs",
        ),
        pytest.param(
            "def consume(path):\n"
            '    return open(path, "rb")\n'
            'consume(*(\"auth.json\",))\n',
            2,
            id="wrapper-call-star-args",
        ),
        pytest.param(
            "def consume(path):\n"
            '    return open(path, "rb")\n'
            'consume(**{"path": "auth.json"})\n',
            2,
            id="wrapper-call-star-star-kwargs",
        ),
        pytest.param(
            "def consume(*args):\n"
            "    return open(*args)\n"
            'consume("auth.json", "rb")\n',
            2,
            id="wrapper-forwards-varargs",
        ),
        pytest.param(
            "def consume(**kwargs):\n"
            "    return open(**kwargs)\n"
            'consume(file="auth.json", mode="rb")\n',
            2,
            id="wrapper-forwards-kwargs",
        ),
    ],
)
def test_audit_rejects_static_auth_store_through_argument_unpacking(
    tmp_path: Path, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", expected_line, "open")
    ]
    assert stale == []

    (tmp_path / "consumer.py").unlink()
    (tmp_path / "harmless.py").write_text(
        source.replace('"auth.json"', '"other.json"'),
        encoding="utf-8",
    )
    assert module.scan_repository(tmp_path) == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        pytest.param(
            "class Consumer:\n"
            "    @staticmethod\n"
            "    def read(path):\n"
            '        return open(path, "rb")\n'
            'Consumer.read("auth.json")\n',
            4,
            id="staticmethod-wrapper",
        ),
        pytest.param(
            "class Consumer:\n"
            "    def read(self, path):\n"
            '        return open(path, "rb")\n'
            "consumer = Consumer()\n"
            'consumer.read("auth.json")\n',
            3,
            id="instance-method-wrapper",
        ),
        pytest.param(
            'consume = lambda path: open(path, "rb")\n'
            'consume("auth.json")\n',
            1,
            id="lambda-wrapper",
        ),
    ],
)
def test_audit_rejects_static_auth_store_through_callable_wrappers(
    tmp_path: Path, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", expected_line, "open")
    ]
    assert stale == []

    (tmp_path / "consumer.py").unlink()
    (tmp_path / "harmless.py").write_text(
        source.replace('"auth.json"', '"other.json"'),
        encoding="utf-8",
    )
    assert module.scan_repository(tmp_path) == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        pytest.param(
            "from pathlib import Path\n"
            "def consume(path):\n"
            "    return Path(path)\n"
            'consume("auth.json")\n',
            4,
            id="function-returns-path",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "consume = lambda path: Path(path)\n"
            'consume("auth.json")\n',
            3,
            id="lambda-returns-path",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "def consume(stem, suffix):\n"
            '    return Path(f"{stem}.{suffix}")\n'
            'consume("auth", "json")\n',
            4,
            id="function-constructs-path-from-split-arguments",
        ),
    ],
)
def test_audit_rejects_auth_path_constructed_by_wrapper(
    tmp_path: Path, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("consumer.py", expected_line, "constructed_path")
    ]

    (tmp_path / "consumer.py").unlink()
    (tmp_path / "harmless.py").write_text(
        source.replace('"auth.json"', '"other.json"').replace(
            '"auth", "json"', '"other", "json"'
        ),
        encoding="utf-8",
    )
    assert module.scan_repository(tmp_path) == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        pytest.param(
            "from pathlib import Path\n"
            'Path(*(\"auth.json\",))\n',
            2,
            id="direct-path-star-args",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "def consume(parts):\n"
            "    return Path(*parts)\n"
            'consume((\"auth.json\",))\n',
            4,
            id="wrapper-path-star-args",
        ),
    ],
)
def test_audit_rejects_static_auth_store_in_starred_path_constructor(
    tmp_path: Path, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("consumer.py", expected_line, "constructed_path")
    ]

    (tmp_path / "consumer.py").unlink()
    (tmp_path / "harmless.py").write_text(
        source.replace('"auth.json"', '"other.json"'),
        encoding="utf-8",
    )
    assert module.scan_repository(tmp_path) == []


def test_starred_path_constructor_fails_closed_on_sequence_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_MAX_STRUCTURED_ALTERNATIVES", 1)
    (tmp_path / "consumer.py").write_text(
        "from pathlib import Path\n"
        'parts = ("other.json",) if enabled else ("auth.json",)\n'
        "Path(*parts)\n",
        encoding="utf-8",
    )

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("consumer.py", 3, "constructed_path")
    ]


def test_scan_ignores_user_call_that_only_returns_auth_filename(
    tmp_path: Path,
) -> None:
    module = _load_module()
    (tmp_path / "harmless.py").write_text(
        'def provider_label():\n    return "auth.json"\n'
        "label = provider_label()\n",
        encoding="utf-8",
    )

    assert module.scan_repository(tmp_path) == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        pytest.param(
            'def target():\n    return "auth.json"\n'
            "target().read_text()\n",
            3,
            id="literal-return",
        ),
        pytest.param(
            "def target(stem, suffix):\n"
            '    return f"{stem}.{suffix}"\n'
            'target("auth", "json").read_text()\n',
            3,
            id="constructed-return",
        ),
    ],
)
def test_audit_rejects_io_on_auth_store_returned_by_wrapper(
    tmp_path: Path, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", expected_line, "read_text")
    ]
    assert stale == []

    (tmp_path / "consumer.py").unlink()
    (tmp_path / "harmless.py").write_text(
        source.replace('"auth.json"', '"other.json"').replace('"auth"', '"other"'),
        encoding="utf-8",
    )
    assert module.scan_repository(tmp_path) == []


def test_direct_wrapper_analysis_memoizes_duplicated_depth_twenty_graph() -> None:
    module = _load_module()
    depth = 20
    definitions = []
    for index in range(depth):
        definitions.append(
            f"def hop_{index}(path):\n"
            f"    hop_{index + 1}(path)\n"
            f"    hop_{index + 1}(path)\n"
        )
    definitions.append(
        f"def hop_{depth}(path):\n"
        '    return open(path, "rb")\n'
    )
    source = "".join(definitions) + 'hop_0("auth.json")\n'
    analyzer = module._PythonFlowAnalyzer("consumer.py")

    findings = analyzer.analyze(ast.parse(source))

    assert [(item.path, item.kind) for item in findings] == [
        ("consumer.py", "open")
    ]
    assert analyzer.direct_function_work <= depth + 1


def test_direct_wrapper_analysis_rejects_depth_twenty_one_graph() -> None:
    module = _load_module()
    depth = 21
    definitions = []
    for index in range(depth):
        definitions.append(
            f"def hop_{index}(path):\n"
            f"    return hop_{index + 1}(path)\n"
        )
    definitions.append(
        f"def hop_{depth}(path):\n"
        '    return open(path, "rb")\n'
    )
    source = "".join(definitions) + 'hop_0("auth.json")\n'

    findings = module._PythonFlowAnalyzer("consumer.py").analyze(ast.parse(source))

    assert "analysis_overflow" in {item.kind for item in findings}


def test_direct_wrapper_analysis_fails_closed_when_work_budget_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_MAX_DIRECT_CALL_WORK", 0)
    (tmp_path / "consumer.py").write_text(
        'def consume(path):\n    return open(path, "rb")\n'
        'consume("auth.json")\n',
        encoding="utf-8",
    )

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("consumer.py", 3, "analysis_overflow")
    ]


def test_direct_wrapper_memoization_includes_environment_state(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(
        'target_name = "other.json"\n'
        "def target():\n"
        "    return target_name\n"
        "target().read_text()\n"
        'target_name = "auth.json"\n'
        "target().read_text()\n",
        encoding="utf-8",
    )

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("consumer.py", 6, "read_text")
    ]


@pytest.mark.parametrize(
    ("source", "expected_line", "expected_kind"),
    [
        pytest.param(
            "from builtins import open as auth_open\n"
            'auth_open("auth.json", "rb")\n',
            2,
            "open",
            id="imported-builtin-open-alias",
        ),
        pytest.param(
            "import builtins as builtin_api\n"
            'builtin_api.open("auth.json", "rb")\n',
            2,
            "open",
            id="builtins-module-alias",
        ),
        pytest.param(
            "from pathlib import Path as AuthPath\n"
            'AuthPath("auth.json").read_text()\n',
            2,
            "read_text",
            id="imported-path-alias",
        ),
        pytest.param(
            "import pathlib as path_api\n"
            'path_api.Path("auth.json").read_text()\n',
            2,
            "read_text",
            id="pathlib-module-alias",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "def load_auth():\n"
            '    auth_store = "auth.json"\n'
            "    return Path(auth_store).read_text()\n",
            4,
            "read_text",
            id="function-local-auth-store-binding",
        ),
    ],
)
def test_audit_rejects_aliased_or_function_local_auth_store_consumers(
    tmp_path: Path, source: str, expected_line: int, expected_kind: str
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", expected_line, expected_kind)
    ]
    assert stale == []


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        pytest.param(
            'auth_open = open\nauth_open("auth.json", "rb")\n',
            "open",
            id="assigned-builtin-open",
        ),
        pytest.param(
            "import builtins\n"
            "auth_open = builtins.open\n"
            'auth_open("auth.json", "rb")\n',
            "open",
            id="assigned-builtins-open",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "AuthPath = Path\n"
            'AuthPath("auth.json").read_text()\n',
            "read_text",
            id="assigned-path-constructor",
        ),
        pytest.param(
            "import pathlib\n"
            "AuthPath = pathlib.Path\n"
            'AuthPath("auth.json").read_text()\n',
            "read_text",
            id="assigned-pathlib-constructor",
        ),
    ],
)
def test_audit_rejects_ordinary_assignment_aliases(
    tmp_path: Path, source: str, expected_kind: str
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.kind) for item in unclassified] == [
        ("consumer.py", expected_kind)
    ]
    assert stale == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        pytest.param(
            "from pathlib import Path\n"
            'AUTH = "auth.json"\n'
            "def read_auth():\n"
            "    global AUTH\n"
            "    value = Path(AUTH).read_text()\n"
            "    AUTH = input()\n"
            "    return value\n",
            5,
            id="global-static-read-before-rebind",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "def outer():\n"
            '    auth_store = "auth.json"\n'
            "    def read_auth():\n"
            "        nonlocal auth_store\n"
            "        value = Path(auth_store).read_text()\n"
            "        auth_store = input()\n"
            "        return value\n"
            "    return read_auth\n",
            6,
            id="nonlocal-static-read-before-rebind",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "def read_auth(enabled):\n"
            "    if enabled:\n"
            '        target = "auth.json"\n'
            "    else:\n"
            "        target = input()\n"
            "    return Path(target).read_text()\n",
            7,
            id="control-flow-static-possibility",
        ),
        pytest.param(
            "from pathlib import Path\n"
            'def read_auth(target="auth.json"):\n'
            "    return Path(target).read_text()\n",
            3,
            id="static-default-argument",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "class Reader:\n"
            "    Path = object()\n"
            "    def read_auth(self):\n"
            '        return Path("auth.json").read_text()\n',
            5,
            id="method-skips-class-namespace",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "class Reader:\n"
            '    auth = Path("auth.json").read_text()\n'
            "    Path = object()\n",
            3,
            id="class-body-uses-sequential-bindings",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "def read_auth():\n"
            "    global AUTH\n"
            "    return Path(AUTH).read_text()\n"
            'AUTH = "auth.json"\n',
            4,
            id="global-binding-assigned-after-function-definition",
        ),
        pytest.param(
            "if enabled:\n"
            "    def read_auth():\n"
            "        return AUTH.read_text()\n"
            'AUTH = "auth.json"\n',
            3,
            id="branch-function-uses-later-module-binding",
        ),
        pytest.param(
            "class Reader:\n"
            "    def read_auth(self):\n"
            "        return AUTH.read_text()\n"
            'AUTH = "auth.json"\n',
            3,
            id="method-uses-later-module-binding",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "def outer():\n"
            "    def read_auth():\n"
            "        return Path(AUTH).read_text()\n"
            '    AUTH = "auth.json"\n'
            "    return read_auth\n",
            4,
            id="closure-binding-assigned-after-inner-definition",
        ),
        pytest.param(
            "from pathlib import Path\n"
            'consumer = lambda: Path("auth.json").read_text()\n',
            2,
            id="lambda-consumer",
        ),
        pytest.param(
            'AUTH = "other.json"\n'
            "consumer = lambda: AUTH.read_text()\n"
            'AUTH = "auth.json"\n',
            2,
            id="lambda-late-bound-module-binding",
        ),
    ],
)
def test_audit_rejects_scope_valid_static_bindings(
    tmp_path: Path, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", expected_line, "read_text")
    ]
    assert stale == []


@pytest.mark.parametrize(
    "expression",
    [
        "'/'.join((root, 'auth.json'))",
        "Path(root).joinpath('.'.join(('auth', 'json')))",
        "root + '/{}{}'.format('auth', '.json')",
    ],
)
def test_audit_rejects_joined_and_formatted_auth_store_paths(
    tmp_path: Path, expression: str
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(
        f"from pathlib import Path\nroot = input()\nopen({expression}, 'rb')\n",
        encoding="utf-8",
    )

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", 3, "open")
    ]
    assert stale == []


@pytest.mark.parametrize(
    ("source", "expected_line", "expected_kind"),
    [
        (
            "auth_open = open if enabled else helper\n"
            'auth_open("auth.json", "rb")\n',
            2,
            "open",
        ),
        (
            "from pathlib import Path\n"
            "AuthPath = Path if enabled else Factory\n"
            'AuthPath("auth.json").read_text()\n',
            3,
            "read_text",
        ),
        (
            "from pathlib import Path\n"
            'target = "auth.json" if enabled else "other.json"\n'
            "Path(target).read_text()\n",
            3,
            "read_text",
        ),
        (
            "from pathlib import Path\n"
            'Path("%s.%s" % ("auth", "json")).read_text()\n',
            2,
            "read_text",
        ),
        pytest.param(
            "from pathlib import Path\n"
            'Path("%(stem)s.%(suffix)s" % '
            '{"stem": "auth", "suffix": "json"}).read_text()\n',
            2,
            "read_text",
            id="mapping-percent-format",
        ),
        pytest.param(
            "from pathlib import Path\n"
            'parts = {"stem": "auth", "suffix": "json"}\n'
            'Path("%(stem)s.%(suffix)s" % parts).read_text()\n',
            3,
            "read_text",
            id="mapping-percent-format-static-binding",
        ),
    ],
)
def test_audit_rejects_conditional_and_percent_formatted_auth_paths(
    tmp_path: Path, source: str, expected_line: int, expected_kind: str
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", expected_line, expected_kind)
    ]
    assert stale == []


def test_scan_ignores_mapping_percent_format_of_other_path(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "harmless.py").write_text(
        "from pathlib import Path\n"
        'parts = {"stem": "other", "suffix": "json"}\n'
        'Path("%(stem)s.%(suffix)s" % parts).read_text()\n',
        encoding="utf-8",
    )

    assert module.scan_repository(tmp_path) == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        pytest.param(
            "class Base:\n"
            "    def read(self, path):\n"
            '        return open(path, "rb")\n'
            "class Child(Base):\n"
            "    pass\n"
            'Child().read("auth.json")\n',
            3,
            id="inherited-instance-method",
        ),
        pytest.param(
            "class Consumer:\n"
            "    def __call__(self, path):\n"
            '        return open(path, "rb")\n'
            'Consumer()("auth.json")\n',
            3,
            id="callable-instance",
        ),
    ],
)
def test_audit_rejects_auth_store_through_object_protocol_wrappers(
    tmp_path: Path, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("consumer.py", expected_line, "open")
    ]

    (tmp_path / "consumer.py").unlink()
    (tmp_path / "harmless.py").write_text(
        source.replace('"auth.json"', '"other.json"'), encoding="utf-8"
    )
    assert module.scan_repository(tmp_path) == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        pytest.param(
            "from pathlib import Path\n"
            'target, other = ("auth.json", "other.json")\n'
            "Path(target).read_text()\n",
            3,
            id="tuple-unpacked-static-path",
        ),
        pytest.param(
            "from pathlib import Path\n"
            'for target in ("auth.json", "other.json"):\n'
            "    Path(target).read_text()\n",
            3,
            id="for-loop-static-path",
        ),
        pytest.param(
            "from pathlib import Path\n"
            '(target := "auth.json")\n'
            "Path(target).read_text()\n",
            3,
            id="walrus-static-path",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "if enabled:\n"
            '    choice = "auth"\n'
            "else:\n"
            '    choice = "other"\n'
            'Path("{}.json".format(choice)).read_text()\n',
            6,
            id="format-preserves-all-branch-alternatives",
        ),
        pytest.param(
            'match value:\n    case _:\n        open("auth.json", "rb")\n',
            3,
            id="match-case-consumer",
        ),
        pytest.param(
            "from pathlib import Path\n"
            '[Path(target).read_text() for target in ("auth.json",)]\n',
            2,
            id="comprehension-static-path",
        ),
        pytest.param(
            "from pathlib import Path\n"
            '[(target := "auth.json") for _ in values]\n'
            "Path(target).read_text()\n",
            3,
            id="comprehension-walrus-binds-containing-scope",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "try:\n"
            '    target = "auth.json"\n'
            "    operation()\n"
            "    target = input()\n"
            "except Exception:\n"
            "    Path(target).read_text()\n",
            7,
            id="except-handler-sees-try-prefix-binding",
        ),
    ],
)
def test_audit_rejects_static_assignment_expression_variants(
    tmp_path: Path, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(source, encoding="utf-8")

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line) for item in unclassified] == [
        ("consumer.py", expected_line)
    ]
    assert stale == []


@pytest.mark.parametrize(
    ("alternative_count", "expected_kind"),
    [(127, None), (131, "read_text")],
)
def test_mapping_percent_alternative_boundary(
    tmp_path: Path, alternative_count: int, expected_kind: str | None
) -> None:
    module = _load_module()
    source = ["from pathlib import Path\n", "stem = 'safe_0'\n"]
    for index in range(1, alternative_count):
        source.append(f"if flag_{index}:\n    stem = 'safe_{index}'\n")
    source.extend(
        [
            "parts = {'stem': stem, 'suffix': 'json'}\n",
            "Path('%(stem)s.%(suffix)s' % parts).read_text()\n",
        ]
    )
    (tmp_path / "consumer.py").write_text("".join(source), encoding="utf-8")

    findings = module.scan_repository(tmp_path)

    assert [item.kind for item in findings] == (
        [] if expected_kind is None else [expected_kind]
    )


def test_static_alternative_overflow_is_bounded_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_MAX_FLOW_ALTERNATIVES", 4)
    (tmp_path / "consumer.py").write_text(
        "from pathlib import Path\n"
        "if one:\n    left = 'a'\nelse:\n    left = 'b'\n"
        "if two:\n    middle = 'c'\nelse:\n    middle = 'd'\n"
        "if three:\n    right = 'e'\nelse:\n    right = 'f'\n"
        "Path('{}{}{}'.format(left, middle, right)).read_text()\n",
        encoding="utf-8",
    )

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", 14, "read_text")
    ]
    assert stale == []


@pytest.mark.parametrize(
    "source",
    [
        'def harmless(open):\n    return open("auth.json", "rb")\n',
        'def harmless(Path):\n    return Path("auth.json").read_text()\n',
        "def installs_alias():\n"
        "    from builtins import open as auth_open\n"
        "    return auth_open\n"
        "def harmless():\n"
        '    return auth_open("auth.json", "rb")\n',
        'def open(path, mode):\n    return (path, mode)\nopen("auth.json", "rb")\n',
        'open = object()\nopen("auth.json", "rb")\n',
        "from pathlib import Path\n"
        "class Reader:\n"
        "    AuthPath = Path\n"
        "    def harmless(self):\n"
        '        return AuthPath("auth.json").read_text()\n',
        'consumer = lambda open: open("auth.json", "rb")\n',
        'AUTH = "auth.json"\n'
        "consumer = lambda: AUTH.read_text()\n"
        'AUTH = "other.json"\n',
        '[open("auth.json", "rb") for open in funcs]\n',
        "from pathlib import Path\n"
        "try:\n"
        "    operation()\n"
        "except Exception as Path:\n"
        '    Path("auth.json").read_text()\n',
        "def harmless():\n"
        '    open("auth.json", "rb")\n'
        "    try:\n"
        "        operation()\n"
        "    except Exception as open:\n"
        "        pass\n",
        'consumer = lambda: (open("auth.json", "rb"), (open := factory()))\n',
        "match value:\n"
        "    case open:\n"
        "        pass\n"
        'open("auth.json", "rb")\n',
    ],
)
def test_scan_ignores_scope_shadowed_or_unrelated_callables(
    tmp_path: Path, source: str
) -> None:
    module = _load_module()
    (tmp_path / "harmless.py").write_text(source, encoding="utf-8")

    assert module.scan_repository(tmp_path) == []


def test_scan_ignores_deferred_function_from_impossible_sibling_branch(
    tmp_path: Path,
) -> None:
    module = _load_module()
    (tmp_path / "harmless.py").write_text(
        "from pathlib import Path\n"
        "if enabled:\n"
        "    def read_auth():\n"
        "        return Path(AUTH).read_text()\n"
        '    AUTH = "other.json"\n'
        "else:\n"
        '    AUTH = "auth.json"\n',
        encoding="utf-8",
    )

    assert module.scan_repository(tmp_path) == []


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "from pathlib import Path\n"
            "if enabled:\n"
            "    def build(path):\n"
            "        return Path(path)\n"
            '    build("other.json")\n'
            "else:\n"
            '    target = "auth.json"\n',
            id="constructed-wrapper",
        ),
        pytest.param(
            "if enabled:\n"
            "    class Base:\n"
            "        def read(self, path):\n"
            '            return open(path, "rb")\n'
            "    class Child(Base):\n"
            "        pass\n"
            '    Child().read("other.json")\n'
            "else:\n"
            '    target = "auth.json"\n',
            id="inherited-method",
        ),
        pytest.param(
            "from pathlib import Path\n"
            "if enabled:\n"
            '    parts = {"stem": "other", "suffix": "json"}\n'
            '    Path("%(stem)s.%(suffix)s" % parts).read_text()\n'
            "else:\n"
            '    parts = {"stem": "auth", "suffix": "json"}\n',
            id="mapping-percent-binding",
        ),
    ],
)
def test_scan_ignores_new_wrapper_flows_from_impossible_sibling_branch(
    tmp_path: Path, source: str
) -> None:
    module = _load_module()
    (tmp_path / "harmless.py").write_text(source, encoding="utf-8")

    assert module.scan_repository(tmp_path) == []


@pytest.mark.parametrize(
    ("filename", "source", "expected_line"),
    [
        ("consumer.mjs", "const p = path.join(root, 'auth' + '.json');\n", 1),
        ("consumer.ts", "const p = `auth` + `.json`;\n", 1),
        ("consumer.cjs", "const p = 'auth' +\n  '.json';\n", 1),
        ("consumer.sh", "target=$HERMES_HOME/'auth'\".json\"\n", 1),
        ("continued.sh", "target=$HERMES_HOME/'auth'\\\n'.json'\n", 1),
        ("consumer.nix", 'target = stateDir + "/auth" + ".json";\n', 1),
    ],
)
def test_scan_rejects_split_non_python_auth_store_paths(
    tmp_path: Path, filename: str, source: str, expected_line: int
) -> None:
    module = _load_module()
    (tmp_path / filename).write_text(source, encoding="utf-8")

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        (filename, expected_line, "text_reference")
    ]


def test_split_non_python_fragment_overflow_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_MAX_TEXT_FRAGMENT_CHAIN", 2)
    (tmp_path / "consumer.mjs").write_text(
        "const target = 'safe' + '' + '' + '.json';\n",
        encoding="utf-8",
    )

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("consumer.mjs", 1, "text_reference")
    ]


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("harmless.mjs", "// auth.json is managed by the authority\n"),
        ("harmless.mjs", "/* auth.json is managed by the authority */\n"),
        ("harmless.sh", "# auth.json is managed by the authority\n"),
        ("harmless.nix", "# auth.json is managed by the authority\n"),
        ("harmless.sh", "printf '%s %s\\n' 'auth' '.json'\n"),
        ("harmless.nix", 'parts = [ "auth" ".json" ];\n'),
        ("harmless.sh", "printf '%s %s %s\\n' 'auth' + '.json'\n"),
    ],
)
def test_scan_ignores_non_python_comment_only_mentions(
    tmp_path: Path, filename: str, source: str
) -> None:
    module = _load_module()
    (tmp_path / filename).write_text(source, encoding="utf-8")

    assert module.scan_repository(tmp_path) == []


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("read_text", ""),
        ("read_bytes", ""),
        ("write_text", '"replacement"'),
        ("write_bytes", 'b"replacement"'),
        ("open", '"rb"'),
    ],
)
def test_audit_rejects_constant_bound_path_io_consumer(
    tmp_path: Path, method: str, arguments: str
) -> None:
    module = _load_module()
    (tmp_path / "consumer.py").write_text(
        "from pathlib import Path\n"
        'AUTH_STORE = "auth.json"\n'
        f"Path(AUTH_STORE).{method}({arguments})\n",
        encoding="utf-8",
    )

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("consumer.py", 3, method)
    ]
    assert stale == []


def test_inventory_rejects_unapproved_category(tmp_path: Path) -> None:
    module = _load_module()
    inventory = _inventory(
        tmp_path,
        {"consumer.py": _entry("looks_safe", "canonical_auth_authority")},
    )

    with pytest.raises(ValueError, match="unapproved category"):
        module.load_inventory(inventory)


def test_inventory_rejects_unapproved_reason(tmp_path: Path) -> None:
    module = _load_module()
    inventory = _inventory(
        tmp_path,
        {
            "consumer.py": _entry(
                "canonical_authority_owner", "reviewed_by_someone"
            )
        },
    )

    with pytest.raises(ValueError, match="unapproved reason"):
        module.load_inventory(inventory)


def test_scan_rejects_missing_root(tmp_path: Path) -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="scan root"):
        module.scan_repository(tmp_path / "missing")


def test_inventory_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    module = _load_module()
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps({"version": 2, "consumers": {}, "allow_all": True}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly version and consumers"):
        module.load_inventory(inventory)
