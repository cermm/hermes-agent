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
