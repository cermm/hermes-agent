"""Real Linux namespace and seccomp probes; no workers, network or paid APIs."""
from dataclasses import replace
import json
import os
from pathlib import Path
import py_compile
import runpy
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from hermes_cli.worker_guard import GuardError, GuardSpec, ProcessIdentity, launch_guard, renew_lease, reconcile_guard


def _require_namespace_probe_success(result):
    if result.returncode == 0:
        return
    denied = {
        "unshare: unshare failed: Operation not permitted",
        "unshare: write failed /proc/self/uid_map: Operation not permitted",
    }
    if result.returncode == 1 and result.stderr.strip() in denied:
        pytest.skip("Host policy denies required user/mount/net/PID namespaces: " + result.stderr.strip())
    raise AssertionError(f"Unexpected namespace capability probe failure: {result.returncode}: {result.stderr}")


@pytest.fixture(scope="session")
def namespace_capability():
    result = subprocess.run([
        "/usr/bin/unshare", "--user", "--map-root-user", "--mount", "--net", "--pid",
        "--fork", "--kill-child=KILL", "/usr/bin/true",
    ], capture_output=True, text=True, encoding="utf-8", timeout=10,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
    _require_namespace_probe_success(result)


@pytest.mark.parametrize("diagnostic", [
    "unshare: unshare failed: Operation not permitted",
    "unshare: write failed /proc/self/uid_map: Operation not permitted",
])
def test_namespace_capability_skips_only_independent_known_host_denial(diagnostic):
    with pytest.raises(pytest.skip.Exception, match="Host policy denies"):
        _require_namespace_probe_success(subprocess.CompletedProcess([], 1, "", diagnostic))
    _require_namespace_probe_success(subprocess.CompletedProcess([], 0, "", ""))
    for code, message in ((2, diagnostic), (1, "guardian containment failed"), (1, "unshare: unexpected failure")):
        with pytest.raises(AssertionError, match="Unexpected namespace"):
            _require_namespace_probe_success(subprocess.CompletedProcess([], code, "", message))


@pytest.mark.linux_only
def test_kernel_denied_namespace_keeps_production_launch_closed(tmp_path):
    from dataclasses import asdict

    spec = make_spec(tmp_path, "raise AssertionError('denied namespace launched worker')")
    source = Path(__file__).parents[2]
    script = tmp_path / "deny-namespace.py"
    script.write_text(
        "import ctypes,json,os,socket,sys\n"
        + f"sys.path.insert(0,{str(source)!r})\n"
        + "from hermes_cli.worker_guard import GuardError,GuardSpec,ProcessIdentity,renew_lease,launch_guard\n"
        + f"data=json.loads({json.dumps(asdict(spec))!r})\n"
        + "data['controller']=ProcessIdentity.capture(os.getpid())\nspec=GuardSpec(**data)\n"
        + "libc=ctypes.CDLL(None,use_errno=True)\nassert libc.prctl(38,1,0,0,0)==0\n"
        + "seccomp=ctypes.CDLL('libseccomp.so.2')\n"
        + "seccomp.seccomp_init.argtypes=[ctypes.c_uint]\nseccomp.seccomp_init.restype=ctypes.c_void_p\n"
        + "seccomp.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p]\n"
        + "seccomp.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_uint]\n"
        + "seccomp.seccomp_load.argtypes=[ctypes.c_void_p]\n"
        + "context=seccomp.seccomp_init(0x7fff0000)\nassert context\n"
        + "call=seccomp.seccomp_syscall_resolve_name(b'unshare')\nassert call>=0\n"
        + "assert seccomp.seccomp_rule_add(context,0x00050001,call,0)==0\n"
        + "assert seccomp.seccomp_load(context)==0\n"
        + "server,worker=socket.socketpair()\nrenew_lease(spec.lease_path,spec,5)\n"
        + "try:\n    launch_guard(spec,worker.fileno())\n"
        + "except GuardError as error:\n    assert 'Operation not permitted' in str(error),str(error)\n    print('kernel-denied-launch-held')\n"
        + "else:\n    raise AssertionError('namespace denial admitted worker')\n"
        + "finally:\n    server.close()\n    worker.close()\n",
        encoding="utf-8")
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kernel-denied-launch-held"
    assert not (Path(spec.state_dir) / "artifacts").exists()


def test_unsupported_host_refuses_identity_and_lease_before_filesystem(monkeypatch):
    from hermes_cli import worker_guard

    def unexpected_read(*args, **kwargs):
        raise AssertionError("unsupported host reached filesystem")

    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "platform", "win32")
        scoped.setattr(Path, "read_text", unexpected_read)
        scoped.setattr(os, "open", unexpected_read)
        with pytest.raises(GuardError, match="Linux process identity required"):
            ProcessIdentity.capture(os.getpid())
        assert worker_guard.lease_valid(None) is False


def test_private_helper_refuses_unsupported_host_before_dependency_loading(monkeypatch):
    helper = Path(__file__).parents[2] / "hermes_cli" / "worker_guard_child.py"
    original_dependency = sys.modules.get("worker_guard")
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "platform", "win32")
        with pytest.raises(RuntimeError, match="Linux namespace supervisor required"):
            runpy.run_path(str(helper), run_name="guardian_platform_probe")
    assert sys.modules.get("worker_guard") is original_dependency


@pytest.mark.linux_only
def test_unicode_lease_json_is_read_as_utf8(tmp_path):
    from hermes_cli.worker_guard import lease_valid

    spec = replace(make_spec(tmp_path, "pass"), permit_id="práca-修复")
    renew_lease(spec.lease_path, spec, ttl=5)
    path = Path(spec.lease_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    assert lease_valid(spec)


def make_spec(tmp_path, code, seconds=12):
    state, workspace = tmp_path / "private", tmp_path / "workspace"
    state.mkdir(mode=0o700)
    workspace.mkdir()
    script = workspace / "probe.py"
    script.write_text(code)
    return GuardSpec(
        permit_id=tmp_path.name, scope={"board": "fixture", "task": "exact", "run": "r1", "generation": 3, "fence": 9},
        command=("/usr/bin/python3", "/workspace/probe.py"),
        read_only_paths=("/usr/bin", "/usr/lib/x86_64-linux-gnu", "/usr/lib/python3.12", "/usr/lib64"),
        workspace=str(workspace), state_dir=str(state), lease_path=str(state / "lease.json"),
        controller=ProcessIdentity.capture(os.getpid()), deadline_monotonic=time.monotonic() + seconds,
        process_limit=16,
    )


def run_probe(spec):
    server, worker = socket.socketpair()
    renew_lease(spec.lease_path, spec, ttl=5)
    stopped = threading.Event()
    child = []
    def maintain_lease():
        while not stopped.wait(.2):
            if child and child[0].poll() is not None:
                return
            renew_lease(spec.lease_path, spec, ttl=2)
    renewal = threading.Thread(target=maintain_lease, daemon=True)
    renewal.start()
    try:
        handle = launch_guard(spec, worker.fileno())
        child.append(handle.process)
        handle.test_stop_lease = stopped
    except BaseException:
        stopped.set()
        renewal.join(timeout=3)
        server.close()
        raise
    finally:
        worker.close()
    return handle, server


def await_file(path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text()
        time.sleep(0.01)
    raise AssertionError(f"missing worker evidence: {path}")


@pytest.mark.linux_only
@pytest.mark.parametrize("operation", ["socket", "host_secret", "readonly_write", "namespace_escape", "broker", "pinned_tmp_config"])
def test_actual_containment_denies_bypass_and_retains_only_broker(tmp_path, operation, namespace_capability):
    secret = tmp_path / "secret"
    secret.write_text("credential-never-mounted")
    checks = {
        "socket": "socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
        "host_secret": f"open({str(secret)!r}).read()",
        "readonly_write": "open('/usr/forbidden-worker-file', 'w')",
        "namespace_escape": "ctypes.CDLL(None, use_errno=True).unshare(0x10000000)",
        "broker": "os.write(int(os.environ['HERMES_WORKER_BROKER_FD']), b'bounded-request')",
        "pinned_tmp_config": f"open({str(tmp_path / 'config.json')!r}).read()",
    }
    code = "import os,socket,ctypes,json\nresult={}\ntry:\n    result['value']=" + checks[operation] + "\nexcept OSError as error:\n    result['errno']=error.errno\nresult['ambient_secret']=os.environ.get('OPENAI_API_KEY')\nopen('/workspace/result.json','w').write(json.dumps(result))\n"
    spec = make_spec(tmp_path, code)
    if operation == "pinned_tmp_config":
        config = tmp_path / "config.json"
        config.write_text('{"model":"fixture-only"}')
        spec = replace(spec, read_only_paths=(*spec.read_only_paths, str(config)))
    handle, server = run_probe(spec)
    try:
        receipt = handle.wait()
        assert receipt["exit_verified"] is True
        assert receipt["artifacts_exported"], (receipt, handle.receipt_path.with_name("stderr.log").read_text())
        result = json.loads(await_file(Path(receipt["artifacts_path"]) / "result.json"))
        assert not (Path(spec.workspace) / "result.json").exists()
        assert result["ambient_secret"] is None
        if operation == "broker":
            server.settimeout(2)
            assert server.recv(100) == b"bounded-request"
        elif operation == "namespace_escape":
            assert result["value"] == -1
        elif operation == "pinned_tmp_config":
            assert json.loads(result["value"]) == {"model": "fixture-only"}
        else:
            assert result["errno"] in (1, 2, 13, 30)
        assert secret.read_text() == "credential-never-mounted"
    finally:
        server.close()
        if handle.process.poll() is None:
            handle.stop()
            handle.wait()


@pytest.mark.linux_only
@pytest.mark.parametrize("failure", ["deadline", "lease", "stop", "guardian", "detached", "broker"])
def test_watchdog_and_kernel_parent_death_end_namespace(tmp_path, failure, namespace_capability):
    code = "import os,signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
    if failure == "detached":
        code += "if os.fork()==0:\n    os.setsid()\n    if os.fork()==0:\n        os.write(int(os.environ['HERMES_WORKER_BROKER_FD']),b'D')\n        while True: time.sleep(1)\n    os._exit(0)\n"
    code += "os.write(int(os.environ['HERMES_WORKER_BROKER_FD']),b'R')\nwhile True: time.sleep(1)\n"
    spec = make_spec(tmp_path, code, seconds=8 if failure in {"deadline", "detached"} else 12)
    handle, server = run_probe(spec)
    try:
        server.settimeout(10)
        evidence = server.recv(2)
        if failure == "detached" and len(evidence) < 2:
            evidence += server.recv(1)
        assert b'R' in evidence
        if failure == "detached":
            assert b'D' in evidence
        initial = json.loads(handle.receipt_path.read_text())
        init = ProcessIdentity(**initial["namespace_init"])
        assert init.alive()
        if failure == "lease":
            handle.test_stop_lease.set()
            time.sleep(.3)
            Path(spec.lease_path).unlink()
        elif failure == "stop":
            handle.stop()
        elif failure == "guardian":
            os.kill(handle.identity.pid, signal.SIGKILL)
        elif failure == "broker":
            server.close()
        handle.process.wait(timeout=8)
        deadline = time.monotonic() + 3
        while init.alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not init.alive(), "namespace init survived guardian/deadline failure"
        if failure != "guardian":
            result = json.loads(handle.receipt_path.read_text())
            assert result["exit_verified"]
            assert result["effects_reconciled"] is False
            assert result["artifacts_exported"] is False
            assert result["reason"] == {"lease": "authority_lease_revoked", "stop": "stop_requested", "broker": "broker_exit"}.get(failure, "deadline")
        else:
            assert json.loads(handle.receipt_path.read_text())["exit_verified"] is False
            recovered = reconcile_guard(spec, handle.receipt_path)
            assert recovered["exit_verified"]
            assert recovered["effects_reconciled"] is False
            assert recovered["artifacts_exported"] is False
    finally:
        server.close()
        if handle.process.poll() is None:
            handle.stop()
            handle.wait()


@pytest.mark.linux_only
@pytest.mark.parametrize("alias", ["direct", "hardlink", "nested_name", "nested_hardlink"])
def test_inert_credential_fixture_cannot_cross_actual_containment(tmp_path, monkeypatch, alias):
    home = tmp_path / "synthetic-home"
    secret = home / ".ssh" / "custom_key"
    secret.parent.mkdir(parents=True)
    secret.write_text("INERT-CREDENTIAL-FIXTURE", encoding="utf-8")
    mount = secret
    if alias == "hardlink":
        mount = tmp_path / "runtime-key"
        os.link(secret, mount)
    exposed = mount
    if alias.startswith("nested_"):
        mount = tmp_path / "selected-runtime"
        exposed = mount / "package" / ("auth.json" if alias == "nested_name" else "ordinary.data")
        exposed.parent.mkdir(parents=True)
        if alias == "nested_hardlink":
            try:
                os.link(secret, exposed)
            except OSError as error:
                if error.errno not in {18, 38, 95}:
                    raise
                pytest.skip("Fixture filesystem does not support hardlinks")
        else:
            exposed.write_bytes(secret.read_bytes())
    spec = make_spec(tmp_path, f"from pathlib import Path\nPath('/workspace/leaked.txt').write_bytes(Path({str(exposed)!r}).read_bytes())")
    spec = replace(spec, read_only_paths=(*spec.read_only_paths, str(mount)))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    try:
        spec.validate()
    except GuardError as error:
        assert "credential" in str(error)
        return
    handle, server = run_probe(spec)
    try:
        receipt = handle.wait()
        leaked = Path(receipt["artifacts_path"]) / "leaked.txt"
        assert leaked.read_text(encoding="utf-8") != "INERT-CREDENTIAL-FIXTURE", "credential fixture escaped into worker artifacts"
    finally:
        server.close()
        if handle.process.poll() is None:
            handle.stop()
            handle.wait()


@pytest.mark.linux_only
@pytest.mark.parametrize("relative", [
    "package/.env", "package/auth.json", "package/credentials", "package/credentials.json",
    "package/id_rsa", "package/id_ed25519", "package/.ssh/custom_key",
    "package/.aws/token", "package/.azure/token", "package/.config/token", "package/.hermes/token",
])
def test_nested_credential_names_are_refused_before_launch(tmp_path, relative):
    mount = tmp_path / "selected-runtime"
    credential = mount / relative
    credential.parent.mkdir(parents=True)
    credential.write_text("inert fixture", encoding="utf-8")
    spec = make_spec(tmp_path, "raise AssertionError('nested credentials must not launch')")
    with pytest.raises(GuardError, match="credential"):
        replace(spec, read_only_paths=(*spec.read_only_paths, str(mount))).validate()


@pytest.mark.linux_only
@pytest.mark.parametrize("kind", ["symlink", "fifo", "late_credential", "completed_child", "completed_mount", "unreadable"])
def test_recursive_runtime_inspection_fails_closed_on_unsafe_nodes_and_changes(tmp_path, monkeypatch, kind):
    mount = tmp_path / "selected-runtime"
    nested = mount / "package"
    nested.mkdir(parents=True)
    safe = nested / "ordinary.data"
    safe.write_text("ordinary fixture", encoding="utf-8")
    if kind == "symlink":
        (nested / "alias").symlink_to(safe)
    elif kind == "fifo":
        os.mkfifo(nested / "pipe")
    elif kind == "unreadable":
        nested.chmod(0)
    else:
        original = os.scandir
        target_identity = (nested.stat().st_dev, nested.stat().st_ino)
        if kind in {"completed_child", "completed_mount"}:
            later = (mount if kind == "completed_child" else tmp_path) / "later"
            later.mkdir()
            (later / "ordinary.data").write_text("later sibling", encoding="utf-8")
            target_identity = (later.stat().st_dev, later.stat().st_ino)

        class MutatingScan:
            def __init__(self, fd, mutate):
                self.scan = original(fd)
                self.mutate = mutate

            def __enter__(self):
                return iter(sorted(self.scan, key=lambda entry: entry.name == "later"))

            def __exit__(self, *args):
                self.scan.close()
                if self.mutate:
                    (nested / "auth.json").write_text("late inert credential", encoding="utf-8")

        def changing_scan(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == target_identity:
                return MutatingScan(fd, True)
            if (info.st_dev, info.st_ino) == (mount.stat().st_dev, mount.stat().st_ino):
                return MutatingScan(fd, False)
            return original(fd)

        monkeypatch.setattr(os, "scandir", changing_scan)
    spec = make_spec(tmp_path, "raise AssertionError('unsafe directory must not launch')")
    mounts = (*spec.read_only_paths, str(mount))
    if kind == "completed_mount":
        mounts = (*mounts, str(later))
    try:
        with pytest.raises(GuardError, match="runtime|credential"):
            replace(spec, read_only_paths=mounts).validate()
    finally:
        nested.chmod(0o700)


@pytest.mark.linux_only
@pytest.mark.parametrize("credential_root", [".hermes", ".ssh", ".aws", ".azure", ".config"])
@pytest.mark.parametrize("alias", [False, True])
def test_credential_descendant_mount_is_refused_before_worker(tmp_path, monkeypatch, credential_root, alias):
    home = tmp_path / "synthetic-home"
    secret = home / credential_root / "custom_key"
    secret.parent.mkdir(parents=True)
    secret.write_text("INERT-CREDENTIAL-FIXTURE", encoding="utf-8")
    mount = secret
    if alias:
        link = tmp_path / "directory-alias"
        link.symlink_to(secret.parent, target_is_directory=True)
        mount = link / secret.name
    spec = make_spec(tmp_path, "raise AssertionError('credential mount must not launch')")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(GuardError, match="credential"):
        replace(spec, read_only_paths=(*spec.read_only_paths, str(mount))).validate()


@pytest.mark.linux_only
@pytest.mark.parametrize("credential_root", [".hermes", ".ssh", ".aws", ".azure", ".config"])
def test_symlinked_credential_root_protects_its_physical_target(tmp_path, monkeypatch, credential_root):
    home = tmp_path / "synthetic-home"
    home.mkdir()
    physical = tmp_path / "physical-credentials"
    physical.mkdir()
    (home / credential_root).symlink_to(physical, target_is_directory=True)
    secret = physical / "custom_key"
    secret.write_text("INERT-CREDENTIAL-FIXTURE", encoding="utf-8")
    spec = make_spec(tmp_path, "pass")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(GuardError, match="credential"):
        replace(spec, read_only_paths=(*spec.read_only_paths, str(secret))).validate()


@pytest.mark.linux_only
def test_safe_home_sibling_remains_mountable(tmp_path, monkeypatch):
    home = tmp_path / "synthetic-home"
    safe = home / ".aws-safe" / "runtime"
    safe.mkdir(parents=True)
    spec = make_spec(tmp_path, "pass")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    replace(spec, read_only_paths=(*spec.read_only_paths, str(safe))).validate()


@pytest.mark.linux_only
@pytest.mark.parametrize("mutation", ["child", "file", "alias", "wrong_command", "other_credential_root"])
def test_hermes_runtime_exception_is_exact_and_cannot_overlap_other_credentials(tmp_path, monkeypatch, mutation):
    import pwd
    from types import SimpleNamespace

    home = tmp_path / "synthetic-home"
    runtime = home / ".hermes" / "hermes-agent" / "venv"
    runtime.mkdir(parents=True)
    executable = runtime / "python"
    executable.symlink_to(sys.executable)
    spec = replace(make_spec(tmp_path, "pass"), command=(str(executable), "-c", "pass"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    monkeypatch.setattr(sys, "prefix", str(runtime))
    monkeypatch.setattr(sys, "executable", str(executable))
    mount = runtime
    if mutation == "child":
        mount = runtime / "subdirectory"
        mount.mkdir()
    elif mutation == "file":
        mount = runtime / "custom_key"
        mount.write_text("INERT", encoding="utf-8")
    elif mutation == "alias":
        alias = tmp_path / "runtime-parent-alias"
        alias.symlink_to(runtime.parent, target_is_directory=True)
        mount = alias / runtime.name
    elif mutation == "wrong_command":
        spec = replace(spec, command=("/usr/bin/python3", "-c", "pass"))
    else:
        (home / ".aws").symlink_to(runtime, target_is_directory=True)
    with pytest.raises(GuardError, match="credential"):
        replace(spec, read_only_paths=(*spec.read_only_paths, str(mount))).validate()


@pytest.mark.linux_only
@pytest.mark.parametrize("mutation", ["parent", "child", "alias", "wrong_command", "other_credential_root", "interpreter_outside_base"])
def test_bundled_runtime_exception_is_exact(tmp_path, monkeypatch, mutation):
    import pwd
    from types import SimpleNamespace

    home = tmp_path / "synthetic-home"
    runtime = home / ".hermes" / "hermes-agent" / "venv"
    runtime.mkdir(parents=True)
    generation = runtime.parent / ".hermes-runtime" / "python" / "generation-fixture"
    base = generation / "cpython-fixture"
    (base / "bin").mkdir(parents=True)
    binary = base / "bin" / "python"
    binary.write_text("inert executable identity fixture", encoding="utf-8")
    executable = runtime / "python"
    executable.symlink_to(binary)
    spec = replace(make_spec(tmp_path, "pass"), command=(str(executable), "-c", "pass"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    monkeypatch.setattr(sys, "prefix", str(runtime))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sys, "executable", str(executable))
    replace(spec, read_only_paths=(*spec.read_only_paths, str(generation))).validate()
    mount = generation
    if mutation == "parent":
        mount = generation.parent
    elif mutation == "child":
        mount = base
    elif mutation == "alias":
        alias = tmp_path / "generation-parent-alias"
        alias.symlink_to(generation.parent, target_is_directory=True)
        mount = alias / generation.name
    elif mutation == "wrong_command":
        spec = replace(spec, command=("/usr/bin/python3", "-c", "pass"))
    elif mutation == "other_credential_root":
        (home / ".ssh").symlink_to(generation, target_is_directory=True)
    else:
        executable.unlink()
        executable.symlink_to("/usr/bin/python3")
    with pytest.raises(GuardError, match="credential"):
        replace(spec, read_only_paths=(*spec.read_only_paths, str(mount))).validate()


@pytest.mark.linux_only
@pytest.mark.parametrize("home_source", ["account", "environment", "path_home"])
def test_actual_trusted_runtime_directory_remains_usable(tmp_path, namespace_capability, monkeypatch, home_source):
    code = "from pathlib import Path\nPath('/workspace/runtime-ok').write_text('trusted runtime executed', encoding='utf-8')"
    spec = make_spec(tmp_path, code)
    spec = replace(spec, command=(sys.executable, "/workspace/probe.py"),
                   read_only_paths=(*spec.read_only_paths, str(Path(sys.prefix)),
                                    str(Path(sys.base_prefix).resolve().parent)))
    if home_source != "account":
        alternate = tmp_path / "alternate-home"
        secret = alternate / ".ssh" / "custom_key"
        secret.parent.mkdir(parents=True)
        secret.write_text("inert alternate-home credential", encoding="utf-8")
        fake_runtime = alternate / ".hermes" / "hermes-agent" / "venv"
        fake_runtime.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(alternate / ".hermes"))
        if home_source == "environment":
            monkeypatch.setenv("HOME", str(alternate))
        else:
            monkeypatch.setattr(Path, "home", classmethod(lambda cls: alternate))
        for forbidden in (secret, fake_runtime):
            with pytest.raises(GuardError, match="credential"):
                replace(spec, read_only_paths=(*spec.read_only_paths, str(forbidden))).validate()
    handle, server = run_probe(spec)
    try:
        receipt = handle.wait()
        assert receipt["exit_verified"] and receipt["artifacts_exported"]
        assert (Path(receipt["artifacts_path"]) / "runtime-ok").read_text(encoding="utf-8") == "trusted runtime executed"
    finally:
        server.close()
        if handle.process.poll() is None:
            handle.stop()
            handle.wait()


@pytest.mark.linux_only
def test_stale_identity_lease_and_credential_configuration_refuse_before_effect(tmp_path):
    spec = make_spec(tmp_path, "raise AssertionError('must never execute')")
    with pytest.raises(GuardError):
        replace(spec, controller=replace(spec.controller, start_ticks=spec.controller.start_ticks + 1)).validate()
    with pytest.raises(GuardError):
        replace(spec, environment={"OPENAI_API_KEY": "no"}).validate()
    with pytest.raises(GuardError):
        replace(spec, read_only_paths=("/",)).validate()
    with pytest.raises(GuardError, match="credential"):
        replace(spec, read_only_paths=(str(Path.home()),)).validate()
    if (Path.home() / ".hermes").exists():
        with pytest.raises(GuardError, match="credential"):
            replace(spec, read_only_paths=(str(Path.home() / ".hermes"),)).validate()
    server, worker = socket.socketpair()
    try:
        with pytest.raises(GuardError, match="lease"):
            launch_guard(spec, worker.fileno())
    finally:
        server.close()
        worker.close()


@pytest.mark.linux_only
@pytest.mark.parametrize("limit", ["fork", "memory"])
def test_actual_kernel_resource_limits_are_conservative_for_tree(tmp_path, limit, namespace_capability):
    if limit == "fork":
        code = "import os,time,json\ncount=0\ntry:\n    for i in range(32):\n        pid=os.fork()\n        if pid==0:\n            while True: time.sleep(1)\n        count+=1\nexcept OSError as error:\n    open('/workspace/result.json','w').write(json.dumps({'count':count,'errno':error.errno}))\n"
    else:
        code = "import json\ntry:\n    value=bytearray(128*1024*1024)\nexcept MemoryError:\n    open('/workspace/result.json','w').write(json.dumps({'bounded':True}))\n"
    spec = replace(make_spec(tmp_path, code), memory_bytes=64 * 1024 * 1024,
                   process_limit=8, aggregate_memory_bytes=512 * 1024 * 1024)
    handle, server = run_probe(spec)
    try:
        receipt = handle.wait()
        assert receipt["exit_verified"]
        result = json.loads(await_file(Path(receipt["artifacts_path"]) / "result.json"))
        if limit == "fork":
            assert result["errno"] == 11
            assert 0 < result["count"] < spec.process_limit
        else:
            assert result["bounded"]
        with pytest.raises(GuardError, match="aggregate"):
            replace(spec, process_limit=9).validate()
    finally:
        server.close()


@pytest.mark.linux_only
def test_parent_death_armed_after_reparenting_cannot_execute(tmp_path):
    """Pause before prctl, kill the original parent, then arm against its PID."""
    helper = Path(__file__).resolve().parents[2] / "hermes_cli" / "worker_guard_child.py"
    ready, release, effect = (tmp_path / name for name in ("ready", "release", "effect"))
    script = tmp_path / "race.py"
    script.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        + f"sys.path.insert(0,{str(helper.parent)!r})\nfrom worker_guard_child import _parent_death\n"
        + "parent=os.getpid()\nchild=os.fork()\nif child==0:\n"
        + f"    Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        + f"    while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
        + "    _parent_death(parent)\n"
        + f"    Path({str(effect)!r}).write_text('bypass')\n    os._exit(0)\n"
        + "while True: time.sleep(1)\n"
    )
    parent = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.DEVNULL)
    child = ProcessIdentity.capture(int(await_file(ready)))
    parent.kill()
    parent.wait(timeout=5)
    release.touch()
    deadline = time.monotonic() + 3
    while child.alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not child.alive()
    assert not effect.exists()


@pytest.mark.linux_only
def test_independent_guardian_observes_controller_death(tmp_path, namespace_capability):
    module_root = Path(__file__).resolve().parents[2]
    script = tmp_path / "controller.py"
    script.write_text(
        "import os,sys,time,socket,json,threading\nfrom pathlib import Path\n"
        + f"sys.path.insert(0,{str(module_root)!r})\nfrom hermes_cli.worker_guard import *\n"
        + f"base=Path({str(tmp_path)!r})\n"
        + "state=base/'private';state.mkdir(mode=0o700)\nworkspace=base/'workspace';workspace.mkdir()\n"
        + "(workspace/'probe.py').write_text('import time\\nwhile True: time.sleep(1)\\n')\n"
        + "spec=GuardSpec('controller-exit',{'run':'exact'},('/usr/bin/python3','/workspace/probe.py'),"
        + "('/usr/bin','/usr/lib/x86_64-linux-gnu','/usr/lib/python3.12','/usr/lib64'),str(workspace),str(state),str(state/'lease.json'),ProcessIdentity.capture(os.getpid()),time.monotonic()+20)\n"
        + "renew_lease(spec.lease_path,spec,5)\ndef renew():\n    while True:\n        renew_lease(spec.lease_path,spec,2)\n        time.sleep(.2)\nthreading.Thread(target=renew,daemon=True).start()\na,b=socket.socketpair()\nhandle=launch_guard(spec,b.fileno())\nb.close()\n"
        + "(base/'ready').write_text(str(handle.receipt_path))\nwhile True: time.sleep(1)\n"
    )
    controller = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.DEVNULL)
    try:
        receipt_path = Path(await_file(tmp_path / "ready"))
        initial = json.loads(receipt_path.read_text())
        controller.kill()
        controller.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("exit_verified"):
                break
            time.sleep(.01)
        assert receipt["exit_verified"]
        assert receipt["reason"] == "controller_exit"
        assert not ProcessIdentity(**initial["namespace_init"]).alive()
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)


@pytest.mark.linux_only
def test_kernel_socket_domain_rejects_mislabeled_connected_network_descriptor(tmp_path):
    spec = make_spec(tmp_path, "raise AssertionError('must never execute')")
    renew_lease(spec.lease_path, spec, 5)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.settimeout(2)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(listener.getsockname())
            accepted, _ = listener.accept()
            with accepted:
                # fromfd's supplied family is not evidence of its kernel domain.
                with pytest.raises(GuardError, match="AF_UNIX"):
                    launch_guard(spec, client.fileno())
    assert not list(Path(spec.state_dir).glob('guard-*'))


@pytest.mark.linux_only
def test_supervisor_and_namespace_ignore_timestamp_valid_stale_bytecode(tmp_path, monkeypatch, namespace_capability):
    import hermes_cli.worker_guard as guard
    copied = tmp_path / "private-code"
    copied.mkdir()
    for name in ("worker_guard.py", "worker_guard_child.py"):
        shutil.copy2(Path(guard.__file__).with_name(name), copied / name)
    source = copied / "worker_guard.py"
    current = source.read_bytes()
    stamp = source.stat().st_mtime
    poisoned = b"raise RuntimeError('stale guardian bytecode executed')\n"
    source.write_bytes(poisoned + b" " * (len(current) - len(poisoned)))
    os.utime(source, (stamp, stamp))
    py_compile.compile(str(source), doraise=True)
    source.write_bytes(current)
    os.utime(source, (stamp, stamp))
    monkeypatch.setattr(guard, "__file__", str(source))
    spec = make_spec(tmp_path, "open('/workspace/result','w').write('current-source')", seconds=12)
    handle, server = run_probe(spec)
    try:
        receipt = handle.wait()
        assert receipt["exit_verified"]
        assert await_file(Path(receipt["artifacts_path"]) / "result") == "current-source"
    finally:
        server.close()


@pytest.mark.linux_only
@pytest.mark.parametrize("resource", ["bytes", "inodes"])
def test_kernel_workspace_aggregate_limits_and_verified_export(tmp_path, resource, namespace_capability):
    code = "import os,json\ncreated=[]\ntry:\n    for index in range(1000):\n        name='/workspace/f'+str(index)\n        created.append(name)\n        with open(name,'wb') as stream:\n"
    code += "            stream.write(b'x'*524288)\n" if resource == "bytes" else "            pass\n"
    code += "except OSError as error:\n    evidence={'errno':error.errno,'files':len(created)}\nelse:\n    evidence={'escaped':True}\nfor name in created:\n    try: os.unlink(name)\n    except FileNotFoundError: pass\nopen('/workspace/result.json','w').write(json.dumps(evidence))\n"
    spec = replace(make_spec(tmp_path, code), workspace_bytes=2 * 1024 * 1024, workspace_inodes=32)
    handle, server = run_probe(spec)
    try:
        receipt = handle.wait()
        assert receipt["exit_verified"] and receipt["artifacts_exported"]
        result = json.loads((Path(receipt["artifacts_path"]) / "result.json").read_text())
        assert result["errno"] == 28
        assert 1 < result["files"] <= (5 if resource == "bytes" else 32)
        assert sorted(p.name for p in Path(spec.workspace).iterdir()) == ["probe.py"]
        from hermes_cli.worker_guard import digest
        assert digest(receipt["artifacts_manifest"]) == receipt["artifacts_manifest_sha256"]
        assert {row["path"] for row in receipt["artifacts_manifest"]} == {"probe.py", "result.json"}
    finally:
        server.close()


@pytest.mark.linux_only
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_untrusted_link_or_special_output_cannot_be_exported(tmp_path, kind, namespace_capability):
    operations = {"symlink": "os.symlink('/usr/bin/python3','/workspace/escape')",
                  "hardlink": "os.link('/workspace/probe.py','/workspace/escape')",
                  "fifo": "os.mkfifo('/workspace/escape')"}
    spec = make_spec(tmp_path, "import os\n" + operations[kind])
    handle, server = run_probe(spec)
    try:
        receipt = handle.wait()
        assert receipt["exit_verified"]
        assert not receipt["artifacts_exported"]
        assert receipt["artifacts_path"] is None
        assert not (Path(spec.state_dir) / "artifacts").exists()
    finally:
        server.close()


@pytest.mark.linux_only
def test_seed_links_are_rejected_before_worker_execution(tmp_path, namespace_capability):
    spec = make_spec(tmp_path, "raise AssertionError('must not run')")
    (Path(spec.workspace) / "secret").symlink_to('/etc/passwd')
    with pytest.raises(GuardError, match="links or special"):
        run_probe(spec)
    assert not (Path(spec.state_dir) / "artifacts").exists()


@pytest.mark.linux_only
def test_deep_output_cannot_exhaust_trusted_export_stack_or_manifest(tmp_path, namespace_capability):
    code = "import os\nfor i in range(140):\n    os.mkdir('d')\n    os.chdir('d')\nopen('result','w').write('untrusted nested output')\n"
    spec = make_spec(tmp_path, code)
    handle, server = run_probe(spec)
    try:
        receipt = handle.wait()
        assert receipt["exit_verified"]
        assert not receipt["artifacts_exported"]
        assert "workspace directory depth limit" in handle.receipt_path.with_name("stderr.log").read_text()
    finally:
        server.close()
