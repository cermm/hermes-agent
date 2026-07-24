from __future__ import annotations

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
