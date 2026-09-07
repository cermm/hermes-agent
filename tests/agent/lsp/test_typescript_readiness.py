"""Prerequisite resolution and real TypeScript post-write diagnostics.

The live test uses typescript-language-server and a compatible tsserver on PATH;
without those optional development dependencies only that test is skipped.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from agent.lsp.servers import ServerContext, find_server_for_file
from agent.lsp.typescript import backend_status, resolve_sdk


def test_sdk_preflight_respects_overrides_workspace_and_profile(tmp_path, monkeypatch, capsys):
    from agent.lsp import cli, install
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    profile = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    binary = profile / "lsp/bin/typescript-language-server"
    binary.parent.mkdir(parents=True)
    binary.write_text("wrapper exists, but SDK is missing")
    binary.chmod(0o755)
    server = find_server_for_file(str(project / "test.ts"))
    ctx = ServerContext(str(project), install_strategy="manual", binary_overrides={"typescript": [str(binary), "--stdio"]})
    assert backend_status(str(project), [str(binary)])["status"] == "sdk-unavailable"
    with pytest.raises(ValueError, match="lib/tsserver.js"):
        server.build_spawn(str(project), ctx)

    def sdk_at(directory):
        (directory / "lib").mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps({"version": "6.0.3"}))
        sdk = directory / "lib/tsserver.js"
        sdk.write_text("SDK entrypoint fixture; no process launched")
        return sdk.resolve()

    staged = sdk_at(profile / "lsp/node_modules/typescript")
    assert Path(server.build_spawn(str(project), ctx).initialization_options["tsserver"]["path"]) == staged
    assert install.try_install("typescript-language-server") == str(binary)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-b"))
    # Install cache must not return the other profile's binary. Unknown/manual
    # strategy prevents network access while still exercising the real cache.
    monkeypatch.setitem(install.INSTALL_RECIPES, "typescript-language-server", {"strategy": "manual", "bin": "absent-test-server"})
    assert install.try_install("typescript-language-server") is None
    assert backend_status(str(project), [str(binary)])["status"] == "sdk-unavailable"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    local = sdk_at(project / "node_modules/typescript")
    assert Path(server.build_spawn(str(project), ctx).initialization_options["tsserver"]["path"]) == local
    options = {"tsserver": {"path": str(staged.parent), "logVerbosity": "off"}, "locale": "en"}
    ctx.init_overrides["typescript"] = options
    spec = server.build_spawn(str(project), ctx)
    assert spec.command == [str(binary), "--stdio"]
    assert spec.initialization_options["tsserver"]["path"] == str(staged)
    assert options["tsserver"]["path"] == str(staged.parent)
    assert spec.initialization_options["locale"] == "en"
    options["tsserver"]["path"] = str(project / "missing.js")
    with pytest.raises(ValueError, match="Configured TypeScript"):
        server.build_spawn(str(project), ctx)
    # Config/profile-aware CLI preflight uses the same explicit override.
    (profile / "config.yaml").write_text(json.dumps({"lsp": {"enabled": False, "servers": {"typescript": {
        "command": [str(binary), "--stdio"], "initialization_options": options}}}}))
    assert cli._typescript_backend_status()["status"] == "sdk-unavailable"
    cli._cmd_status(True)
    report = json.loads(capsys.readouterr().out)
    row = next(row for row in report["registry"] if row["server_id"] == "typescript")
    assert row["backend"]["status"] == "sdk-unavailable"
    assert "tsserver.path" in row["backend"]["message"]


@pytest.mark.parametrize("sdk_selection", ["profile", "explicit-bin"])
def test_real_typescript_cold_warm_write_and_repair(tmp_path, monkeypatch, sdk_selection):
    binary = shutil.which("typescript-language-server")
    tsserver = shutil.which("tsserver")
    sdk = Path(tsserver).resolve().parent.parent / "lib/tsserver.js" if tsserver else None
    if not binary or sdk is None or not sdk.is_file():
        pytest.skip("requires typescript-language-server and compatible tsserver on PATH")
    from agent.lsp import get_service, shutdown_service
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    profile = tmp_path / "profile"
    profile.mkdir()
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    (project / "tsconfig.json").write_text(json.dumps({"compilerOptions": {"strict": True, "noEmit": True}, "include": ["*.ts"]}))
    # Real SDK is read-only; selection uses the temporary profile's managed tree.
    staged = profile / "lsp/node_modules"
    staged.mkdir(parents=True)
    (staged / "typescript").symlink_to(sdk.parent.parent, target_is_directory=True)
    init_options = {"disableAutomaticTypingAcquisition": True}
    if sdk_selection == "explicit-bin":
        init_options["tsserver"] = {"path": tsserver}
    (profile / "config.yaml").write_text(json.dumps({"lsp": {"enabled": True, "install_strategy": "manual",
        "wait_timeout": 5, "servers": {"typescript": {"command": [binary, "--stdio"],
        "initialization_options": init_options}}}}))
    shutdown_service()
    operations = ShellFileOperations(LocalEnvironment(cwd=str(project)))
    source = project / "probe.ts"
    good = "export const count: number = 1;\n"
    bad = 'export const count: number = "bad";\n'
    source.write_text(good)
    timings = {}
    try:
        status = backend_status(str(project), [binary], init_options)
        assert status["status"] == "prerequisites-present"
        for label, content, expected_error in [("cold-error", bad, True), ("repair", good, False), ("warm-error", bad, True), ("repair-again", good, False)]:
            start = time.monotonic()
            result = operations.write_file(str(source), content)
            timings[label] = round(time.monotonic() - start, 3)
            assert result.verified and not result.error
            assert ("2322" in (result.lsp_diagnostics or "")) == expected_error, result.to_dict()
            svc = get_service()
            client = next(iter(svc._clients.values()))
            assert client.is_running
            doc = client._docs[str(source)]
            assert doc.fresh(), "absence of errors must be an actual fresh verdict"
            assert bool(client.diagnostics_for(str(source), fresh_only=True)) == expected_error
        assert get_service().get_status()["broken"] == []
        print("Real TypeScript write timings:", json.dumps(timings, sort_keys=True))
    finally:
        shutdown_service()


def test_status_and_spawn_agree_on_wrapper_and_supported_sdk_forms(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.chdir(project)
    staged_wrapper = profile / "lsp/bin/typescript-language-server"
    staged_wrapper.parent.mkdir(parents=True)
    staged_wrapper.write_text("staged wrapper without SDK")
    staged_wrapper.chmod(0o755)
    package_tree = tmp_path / "global/node_modules"
    sdk_root = package_tree / "typescript"
    (sdk_root / "lib").mkdir(parents=True)
    (sdk_root / "bin").mkdir()
    (sdk_root / "package.json").write_text(json.dumps({"version": "6.0.3"}))
    sdk = sdk_root / "lib/tsserver.js"
    sdk.write_text("SDK prerequisite fixture")
    sdk_binary = sdk_root / "bin/tsserver"
    sdk_binary.write_text("SDK binary prerequisite fixture")
    sdk_binary.chmod(0o755)
    wrapper = package_tree / "typescript-language-server/bin/typescript-language-server"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("PATH wrapper with SDK")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(wrapper.parent) + os.pathsep + str(sdk_binary.parent))
    server = find_server_for_file(str(project / "test.ts"))
    spec = server.build_spawn(str(project), ServerContext(str(project), install_strategy="manual"))
    status = backend_status(str(project))
    assert status["status"] == "prerequisites-present"
    assert status["binary"] == spec.command[0] == str(wrapper)
    assert status["sdk_path"] == spec.initialization_options["tsserver"]["path"] == str(sdk)
    unrelated_file = sdk_root / "bin/unrelated"
    unrelated_file.write_text("not a tsserver launcher")
    for option_name in ("path", "fallbackPath"):
        for supported in (str(sdk_binary), "tsserver", str(sdk_root), str(sdk.parent), str(sdk)):
            resolved, _source = resolve_sdk(str(project), str(wrapper), {"tsserver": {option_name: supported}})
            assert resolved == str(sdk), supported
        for invalid in (str(sdk_root / "missing"), str(sdk_root / "missing.js"), str(project), str(unrelated_file)):
            with pytest.raises(ValueError, match="Configured TypeScript"):
                resolve_sdk(str(project), str(wrapper), {"tsserver": {option_name: invalid}})
    # Discovery without either wrapper must not invoke any installer from status.
    monkeypatch.setenv("PATH", "")
    staged_wrapper.unlink()
    assert backend_status(str(project))["status"] == "binary-missing"
    untouched_profile = tmp_path / "untouched-profile"
    monkeypatch.setenv("HERMES_HOME", str(untouched_profile))
    assert backend_status(str(project))["status"] == "binary-missing"
    assert not untouched_profile.exists()
