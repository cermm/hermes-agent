"""Linux namespace containment for an explicitly authorized bounded worker.

This is an execution boundary, not another task authority. The caller renews a
short private lease only after checking its canonical fence. A separate process
supervises the lease and controller; kernel parent-death and PID namespace rules
contain descendants even if that supervisor is killed without running cleanup.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Any


class GuardError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    boot_id: str

    @classmethod
    def capture(cls, pid: int) -> "ProcessIdentity":
        if sys.platform != "linux":
            raise GuardError("Linux process identity required")
        if isinstance(pid, bool) or pid <= 0:
            raise GuardError("invalid process identity")
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2:].split()
        if fields[0] == "Z":
            raise GuardError("process already exited")
        return cls(pid, int(fields[19]), Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip())

    def alive(self) -> bool:
        try:
            return self == self.capture(self.pid)
        except (OSError, ValueError, GuardError):
            return False


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class GuardSpec:
    permit_id: str
    scope: dict[str, Any]
    command: tuple[str, ...]
    read_only_paths: tuple[str, ...]
    workspace: str
    state_dir: str
    lease_path: str
    controller: ProcessIdentity
    deadline_monotonic: float
    environment: dict[str, str] = field(default_factory=dict)
    memory_bytes: int = 536870912
    process_limit: int = 16
    aggregate_memory_bytes: int = 8589934592
    workspace_bytes: int = 268435456
    workspace_inodes: int = 8192

    @property
    def scope_sha256(self) -> str:
        return digest(self.scope)

    def validate(self) -> None:
        if sys.platform != "linux":
            raise GuardError("Linux pidfd and namespace containment required")
        if not self.permit_id or not self.scope or not self.controller.alive():
            raise GuardError("missing permit, scope, or current controller")
        remaining = self.deadline_monotonic - time.monotonic()
        if not math.isfinite(remaining) or not 0 < remaining <= 3600:
            raise GuardError("deadline must be current and bounded to one hour")
        if not self.command or not Path(self.command[0]).is_absolute():
            raise GuardError("absolute pinned command required")
        if not 16777216 <= self.memory_bytes <= 8589934592 or not 1 <= self.process_limit <= 4096:
            raise GuardError("invalid resource limits")
        if not self.memory_bytes * self.process_limit <= self.aggregate_memory_bytes <= 8589934592:
            raise GuardError("aggregate process/address-space bound exceeds admitted memory")
        if type(self.workspace_bytes) is not int or not 1048576 <= self.workspace_bytes <= 536870912 or type(self.workspace_inodes) is not int or not 16 <= self.workspace_inodes <= 65536:
            raise GuardError("invalid aggregate workspace limits")
        workspace = Path(self.workspace).resolve(strict=True)
        state = Path(self.state_dir).resolve(strict=True)
        lease = Path(self.lease_path).absolute()
        if not workspace.is_dir() or not state.is_dir():
            raise GuardError("workspace and state must exist")
        if state == workspace or state.is_relative_to(workspace) or workspace.is_relative_to(state):
            raise GuardError("state and workspace must be disjoint")
        if lease.parent.resolve() != state:
            raise GuardError("lease must be in private guardian state")
        info = state.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:  # windows-footgun: ok — validate rejects non-Linux before UID checks.
            raise GuardError("guardian state must be owned and private")
        forbidden = {"/", "/home", "/root", "/proc", "/sys", "/dev", "/run", "/tmp", "/etc", "/mnt", "/media"}
        paths = [Path(p).absolute() for p in self.read_only_paths]
        home = Path.home().absolute()
        credential_roots = [(name, home / name, (home / name).resolve())
                            for name in (".hermes", ".ssh", ".aws", ".azure", ".config")]
        # The already-running trusted interpreter is the sole supported runtime
        # exception below .hermes, together with its bundled CPython generation.
        # Admit only those whole canonical directories, never a supplied prefix,
        # alias, individual file or arbitrary descendant.
        runtime = home / ".hermes" / "hermes-agent" / "venv"
        base_runtime = Path(sys.base_prefix).absolute()
        generation = base_runtime.parent
        bundled_parent = home / ".hermes" / "hermes-agent" / ".hermes-runtime" / "python"
        interpreter = Path(sys.executable).resolve(strict=True)
        trusted_interpreter = (Path(sys.prefix).absolute() == runtime
                               and Path(sys.prefix).resolve(strict=True) == runtime
                               and Path(self.command[0]).absolute() == Path(sys.executable).absolute()
                               and Path(self.command[0]).resolve(strict=True) == interpreter
                               and interpreter.is_relative_to(base_runtime.resolve(strict=True)))
        bundled_generation = (generation.parent == bundled_parent
                              and generation.name.startswith("generation-")
                              and generation.resolve(strict=True) == generation
                              and base_runtime.resolve(strict=True) == base_runtime)
        for path in paths:
            resolved = path.resolve(strict=True)
            if str(path) in forbidden or str(resolved) in forbidden or not path.is_absolute():
                raise GuardError("broad host mount forbidden")
            if path.is_symlink() or resolved == state or state.is_relative_to(resolved):
                raise GuardError("symlink or guardian state mount forbidden")
            if home.is_relative_to(path) or home.resolve().is_relative_to(resolved):
                raise GuardError("host home or credential root mount forbidden")
            runtime_mount = (trusted_interpreter and path.is_dir() and resolved == path
                             and (path == runtime or (bundled_generation and path == generation)))
            for name, lexical, physical in credential_roots:
                overlaps = (path.is_relative_to(lexical) or lexical.is_relative_to(path)
                            or resolved.is_relative_to(physical) or physical.is_relative_to(resolved))
                if overlaps and not (name == ".hermes" and runtime_mount):
                    raise GuardError("host home or credential root mount forbidden")
            if resolved.is_file() and resolved.stat().st_nlink != 1:
                raise GuardError("multiply-linked credential file alias mount forbidden")
            if resolved.name in {".env", "auth.json", "credentials", "credentials.json", "id_rsa", "id_ed25519"}:
                raise GuardError("credential file mount forbidden")
            if resolved.is_dir() and any((resolved / name).exists() for name in (".env", "auth.json", "credentials.json", ".ssh", ".aws")):
                raise GuardError("runtime mount contains credential material")
            if resolved == workspace or workspace.is_relative_to(resolved) or resolved.is_relative_to(workspace):
                raise GuardError("workspace must not alias a runtime mount")
            if not (resolved.is_file() or resolved.is_dir()):
                raise GuardError("runtime mount must be a regular file or directory")
        executable = Path(self.command[0]).resolve(strict=True)
        if not any(executable == p.resolve() or executable.is_relative_to(p.resolve()) for p in paths):
            raise GuardError("executable is outside pinned runtime mounts")
        allowed = {"LANG", "LC_ALL", "TZ", "PATH", "HOME", "HERMES_HOME", "HERMES_KANBAN_BOARD", "HERMES_WORKER_BROKER_FD", "PYTHONPATH"}
        if set(self.environment) - allowed or any(not isinstance(v, str) or "\0" in v for v in self.environment.values()):
            raise GuardError("ambient environment or credential override forbidden")


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def renew_lease(path: str | Path, spec: GuardSpec, ttl: float = 2.0) -> None:
    """Trusted controller only: call after canonical ownership/fence readback."""
    if not 0 < ttl <= 5 or Path(path).absolute() != Path(spec.lease_path).absolute():
        raise GuardError("invalid lease destination or interval")
    if ProcessIdentity.capture(os.getpid()) != spec.controller:
        raise GuardError("only exact controller can renew its lease")
    _write_json(Path(path), {
        "permit_id": spec.permit_id, "scope_sha256": spec.scope_sha256,
        "controller": asdict(spec.controller),
        "expires_monotonic": min(time.monotonic() + ttl, spec.deadline_monotonic),
    })


def lease_valid(spec: GuardSpec) -> bool:
    if sys.platform != "linux":
        return False
    try:
        fd = os.open(spec.lease_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or not stat.S_ISREG(info.st_mode):  # windows-footgun: ok — lease_valid rejects non-Linux before UID checks.
                return False
            value = json.load(stream)
        expires = value["expires_monotonic"]
        return (value["permit_id"] == spec.permit_id and value["scope_sha256"] == spec.scope_sha256
                and value["controller"] == asdict(spec.controller)
                and isinstance(expires, (int, float)) and not isinstance(expires, bool)
                and time.monotonic() < expires <= min(time.monotonic() + 5.1, spec.deadline_monotonic))
    except (OSError, ValueError, KeyError, TypeError):
        return False


@dataclass
class GuardHandle:
    process: subprocess.Popen
    identity: ProcessIdentity
    receipt_path: Path
    stop_path: Path

    def stop(self) -> None:
        self.stop_path.touch(mode=0o600, exist_ok=True)

    def wait(self, timeout: float = 10) -> dict[str, Any]:
        self.process.wait(timeout=timeout)
        return json.loads(self.receipt_path.read_text(encoding="utf-8"))


def reconcile_guard(spec: GuardSpec, receipt_path: str | Path) -> dict[str, Any]:
    """Recover exact local exit after supervisor loss; remote effects stay open."""
    expected = Path(spec.state_dir) / ("guard-" + hashlib.sha256(spec.permit_id.encode()).hexdigest()) / "receipt.json"
    if Path(receipt_path).absolute() != expected.absolute():
        raise GuardError("wrong guardian receipt")
    if json.loads(expected.with_name("spec.json").read_text(encoding="utf-8")) != json.loads(json.dumps(asdict(spec))):
        raise GuardError("guardian specification changed")
    value = json.loads(expected.read_text(encoding="utf-8"))
    if value.get("permit_id") != spec.permit_id or value.get("scope_sha256") != spec.scope_sha256:
        raise GuardError("guardian scope changed")
    guardian = ProcessIdentity(**value["guardian"])
    namespace = ProcessIdentity(**value["namespace_init"]) if value.get("namespace_init") else None
    if guardian.alive() or namespace is None or namespace.alive():
        return {**value, "exit_verified": False, "effects_reconciled": False}
    # A different boot also proves that the old PID namespace no longer exists;
    # it never gives the old permit a new monotonic deadline or effect allowance.
    value.update(state="exited", exit_verified=True, effects_reconciled=False,
                 reconciliation="namespace_exit_verified", reconciled_monotonic=time.monotonic())
    _write_json(expected, value)
    return value


def launch_guard(spec: GuardSpec, broker_fd: int) -> GuardHandle:
    """Launch only a contained subprocess; the supplied FD must be connected."""
    import socket
    spec.validate()
    if not lease_valid(spec):
        raise GuardError("fresh canonical-authority lease required")
    probe = socket.fromfd(broker_fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if (probe.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_DOMAIN", 39)) != socket.AF_UNIX
                or probe.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM):
            raise GuardError("connected AF_UNIX broker channel required")
        probe.getpeername()
    finally:
        probe.close()
    state = Path(spec.state_dir)
    if any((state / name).exists() for name in ("artifacts", "artifacts-pending", "artifacts-receipt.json")):
        raise GuardError("fresh artifact export destination required")
    run = state / ("guard-" + hashlib.sha256(spec.permit_id.encode()).hexdigest())
    run.mkdir(mode=0o700)
    spec_path, receipt, stop = run / "spec.json", run / "receipt.json", run / "stop"
    _write_json(spec_path, asdict(spec))
    ready_read, ready_write = os.pipe()
    helper = Path(__file__).with_name("worker_guard_child.py")
    error_log = open(run / "stderr.log", "xb")
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", str(helper), "supervise", str(spec_path), str(broker_fd), str(ready_write)],
            pass_fds=(broker_fd, ready_write), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=error_log,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, start_new_session=True,
        )
    finally:
        error_log.close()
        os.close(ready_write)
    try:
        identity = ProcessIdentity.capture(process.pid)
        readable, _, _ = select.select([ready_read], [], [], min(10, max(0.1, spec.deadline_monotonic - time.monotonic())))
        result = os.read(ready_read, 65536) if readable else b""
        if not result or json.loads(result).get("state") != "contained":
            process.kill()
            process.wait(timeout=5)
            raise GuardError("containment failed: " + (run / "stderr.log").read_text(encoding="utf-8", errors="replace")[-3000:])
        return GuardHandle(process, identity, receipt, stop)
    finally:
        os.close(ready_read)
