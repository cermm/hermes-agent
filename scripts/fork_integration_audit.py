"""Per-file collection/skip receipts for the fork's bounded canonical CI run."""
from __future__ import annotations

import json
from pathlib import Path
import sys


def pytest_addoption(parser):
    parser.addoption("--fork-ci-report-dir", required=True)


def pytest_configure(config):
    root = Path(config.rootpath).resolve()
    test_file = Path(config.args[0]).resolve()
    relative_file = test_file.relative_to(root).as_posix()
    config._fork_ci_audit = {
        "file": relative_file,
        "collected": [], "windows_only": [], "skipped": [],
        "collection_skips": [], "passed": 0,
    }


def pytest_collection_finish(session):
    audit = session.config._fork_ci_audit
    audit["collected"] = [item.nodeid for item in session.items]
    audit["windows_only"] = [
        item.nodeid for item in session.items
        if item.get_closest_marker("windows_only") is not None
    ]


def pytest_collectreport(report):
    # Reports lack a config reference. Keep only collection skips, which are
    # always unexpected for this Linux lane (including missing optional SDKs).
    if report.skipped:
        _collection_skips.append({"nodeid": report.nodeid, "reason": str(report.longrepr)})


_collection_skips = []
_test_reports = []


def pytest_runtest_logreport(report):
    if report.skipped or (report.when == "call" and report.passed):
        _test_reports.append(report)


def pytest_sessionfinish(session, exitstatus):
    audit = session.config._fork_ci_audit
    audit["platform"] = sys.platform
    audit["exitstatus"] = int(exitstatus)
    audit["collection_skips"] = _collection_skips
    audit["passed"] = sum(r.when == "call" and r.passed for r in _test_reports)
    audit["skipped"] = [
        {"nodeid": r.nodeid, "when": r.when, "reason": str(r.longrepr),
         "xfail": hasattr(r, "wasxfail")}
        for r in _test_reports if r.skipped
    ]
    directory = Path(session.config.getoption("--fork-ci-report-dir"))
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (audit["file"].replace("/", "__") + ".json")
    target.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
