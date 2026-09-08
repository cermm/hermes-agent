"""Private supervisor/namespace entry point for worker_guard (no public CLI)."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time
import types

if sys.platform != "linux":
    raise RuntimeError("Linux namespace supervisor required")

# The entry script itself is compiled from source by Python. Explicitly compile
# its sole first-party dependency too: timestamp-valid __pycache__ is not proof
# of the approved source bytes. The selected interpreter owns stdlib integrity.
_guard_path = Path(__file__).resolve().with_name("worker_guard.py")
_guard_module = types.ModuleType("worker_guard")
_guard_module.__file__ = str(_guard_path)
sys.modules["worker_guard"] = _guard_module
exec(compile(_guard_path.read_bytes(), str(_guard_path), "exec"), _guard_module.__dict__)
GuardSpec = _guard_module.GuardSpec
ProcessIdentity = _guard_module.ProcessIdentity
_write_json = _guard_module._write_json
lease_valid = _guard_module.lease_valid

LIBC = ctypes.CDLL(None, use_errno=True)


def _prctl(option, arg=0):
    if LIBC.prctl(option, arg, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl")


def _parent_death(expected_parent):
    _prctl(1, signal.SIGKILL)  # windows-footgun: ok — module rejects non-Linux before defining syscall paths.
    if os.getppid() != expected_parent:
        os._exit(125)


def _mount(source, target, flags, kind=None, data=None):
    encode = lambda value: None if value is None else os.fsencode(value)
    if LIBC.mount(encode(source), encode(target), encode(kind), ctypes.c_ulong(flags), encode(data)) != 0:
        raise OSError(ctypes.get_errno(), f"mount {target}")


def _bind(source: Path, target: Path, *, readonly: bool):
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    _mount(str(source), str(target), 4096)
    # Bind without MS_REC: nested host mounts are never implicitly admitted.
    _mount(None, str(target), 4096 | 32 | 2 | 4 | (1 if readonly else 0))


def _filesystem(spec: GuardSpec, root: Path):
    _mount(None, "/", (1 << 18) | 16384)  # MS_PRIVATE | MS_REC
    root.mkdir(mode=0o700)
    _mount("tmpfs", root, 2 | 4, "tmpfs", "size=16m,nr_inodes=1024,mode=755")
    # Private tmp must exist before pinning selected files beneath it; mounting
    # it afterwards would either fail mkdir or hide the approved config mount.
    (root / "tmp").mkdir()
    _mount("tmpfs", root / "tmp", 2 | 4, "tmpfs", "size=64m,nr_inodes=4096,mode=1777")
    for source in sorted(map(Path, spec.read_only_paths), key=lambda p: len(p.parts)):
        _bind(source, root / str(source).lstrip("/"), readonly=True)
    for alias, target in (("bin", "usr/bin"), ("sbin", "usr/sbin"), ("lib", "usr/lib"), ("lib64", "usr/lib64")):
        if not (root / alias).exists() and (root / target).exists():
            (root / alias).symlink_to(target)
    (root / "workspace").mkdir()
    _mount("tmpfs", root / "workspace", 2 | 4, "tmpfs",
           f"size={spec.workspace_bytes},nr_inodes={spec.workspace_inodes},mode=700")
    seed = os.open(spec.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    destination = os.open(root / "workspace", os.O_RDONLY | os.O_DIRECTORY)
    try:
        _copy_tree(seed, destination, spec)
    finally:
        os.close(seed)
        os.close(destination)
    (root / "dev").mkdir()
    # Devices are individual preexisting mounts; no host device tree is exposed.
    for name in ("null", "zero", "urandom", "random"):
        target = root / "dev" / name
        target.touch()
        _mount("/dev/" + name, target, 4096)
        _mount(None, target, 4096 | 32 | 2 | (1 if name in {"urandom", "random"} else 0))
    _mount(None, root, 32 | 1 | 2 | 4)
    os.chroot(root)
    os.chdir("/workspace")


def _copy_tree(source, destination, spec):
    """Bounded, no-follow regular-file copy; used only by trusted setup/PID1."""
    manifest, totals = [], [0, 0, 0]
    def visit(src, dst, prefix, depth=0):
        if depth > 128:
            raise RuntimeError("workspace directory depth limit")
        for name in sorted(os.listdir(src)):
            info = os.stat(name, dir_fd=src, follow_symlinks=False)
            totals[1] += 1
            if totals[1] >= spec.workspace_inodes:
                raise RuntimeError("workspace inode export limit")
            relative = prefix + name
            if len(os.fsencode(relative)) > 4096:
                raise RuntimeError("workspace relative path limit")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src)
                try:
                    os.mkdir(name, 0o700, dir_fd=dst)
                    output = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst)
                    try:
                        visit(child, output, relative + "/", depth + 1)
                    finally:
                        os.close(output)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                incoming = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
                try:
                    current = os.fstat(incoming)
                    if (current.st_dev, current.st_ino, current.st_mode, current.st_nlink) != (info.st_dev, info.st_ino, info.st_mode, 1):
                        raise RuntimeError("workspace file identity changed")
                    outgoing = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600 | (info.st_mode & 0o100), dir_fd=dst)
                    try:
                        size, digest = 0, hashlib.sha256()
                        while True:
                            block = os.read(incoming, 65536)
                            if not block:
                                break
                            size += len(block)
                            totals[0] += len(block)
                            if totals[0] > spec.workspace_bytes:
                                raise RuntimeError("workspace byte export limit")
                            digest.update(block)
                            view = memoryview(block)
                            while view:
                                view = view[os.write(outgoing, view):]
                        os.fsync(outgoing)
                        entry = dict(path=relative, bytes=size, sha256=digest.hexdigest())
                        totals[2] += len(json.dumps(entry).encode()) + 2
                        if totals[2] > 4 * 1024 * 1024:
                            raise RuntimeError("workspace manifest size limit")
                        manifest.append(entry)
                    finally:
                        os.close(outgoing)
                finally:
                    os.close(incoming)
            else:
                raise RuntimeError("workspace links or special files forbidden")
    visit(source, destination, "")
    return manifest


def _export(spec, state_fd):
    os.mkdir("artifacts-pending", 0o700, dir_fd=state_fd)
    destination = os.open("artifacts-pending", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=state_fd)
    source = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        manifest = _copy_tree(source, destination, spec)
        os.fsync(destination)
    finally:
        os.close(source)
        os.close(destination)
    os.rename("artifacts-pending", "artifacts", src_dir_fd=state_fd, dst_dir_fd=state_fd)
    receipt = dict(artifacts_exported=True, artifacts_path=str(Path(spec.state_dir) / "artifacts"),
        artifacts_manifest=manifest, artifacts_manifest_sha256=_guard_module.digest(manifest))
    fd = os.open("artifacts-receipt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=state_fd)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(state_fd)


def _restrictions(spec: GuardSpec):
    import resource
    _prctl(38, 1)  # no_new_privs persists across exec
    _prctl(4, 0)  # no core/process inspection during setup
    for capability in range(64):
        if LIBC.prctl(24, capability, 0, 0, 0) != 0 and ctypes.get_errno() != errno.EINVAL:
            raise OSError(ctypes.get_errno(), "drop capability bounding set")
    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]
    header, data = Header(0x20080522, 0), (Data * 2)()
    if LIBC.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "drop capabilities")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (spec.memory_bytes, spec.memory_bytes))
    resource.setrlimit(resource.RLIMIT_NPROC, (spec.process_limit, spec.process_limit))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    class Compare(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint), ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]
    seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Compare)]
    context = seccomp.seccomp_init(0x7FFF0000)
    if not context:
        raise RuntimeError("seccomp unavailable")
    try:
        forbidden = (
            "socket", "connect", "bind", "listen", "accept", "accept4",
            "mount", "umount2", "pivot_root", "chroot", "setns", "unshare",
            "ptrace", "process_vm_readv", "process_vm_writev", "process_madvise",
            "pidfd_getfd", "kcmp",
            "bpf", "perf_event_open", "open_by_handle_at", "name_to_handle_at",
            "io_uring_setup", "io_uring_enter", "io_uring_register", "userfaultfd",
            "keyctl", "add_key", "request_key", "reboot", "kexec_load",
        )
        for name in forbidden:
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and seccomp.seccomp_rule_add(context, 0x50000 | errno.EPERM, number, 0) != 0:
                raise RuntimeError("seccomp rule failed: " + name)
        # glibc may fall back from clone3 to clone; deny each namespace flag.
        clone3 = seccomp.seccomp_syscall_resolve_name(b"clone3")
        if clone3 >= 0 and seccomp.seccomp_rule_add(context, 0x50000 | errno.ENOSYS, clone3, 0) != 0:
            raise RuntimeError("clone3 rule failed")
        clone = seccomp.seccomp_syscall_resolve_name(b"clone")
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000, 0x10000000, 0x20000000, 0x40000000, 0x80):
            comparison = Compare(0, 7, flag, flag)  # SCMP_CMP_MASKED_EQ
            if seccomp.seccomp_rule_add_array(context, 0x50000 | errno.EPERM, clone, 1, ctypes.byref(comparison)) != 0:
                raise RuntimeError("namespace clone rule failed")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("seccomp load failed")
    finally:
        seccomp.seccomp_release(context)


def _spec(path: str) -> GuardSpec:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    value["controller"] = ProcessIdentity(**value["controller"])
    value["command"] = tuple(value["command"])
    value["read_only_paths"] = tuple(value["read_only_paths"])
    return GuardSpec(**value)


def _namespace(spec_path: str, broker_fd: int, ready_fd: int):
    spec = _spec(spec_path)
    # /proc currently belongs to the outer namespace. Record our kernel identity
    # before dropping it, without exposing an outer /proc descriptor to workers.
    raw = Path("/proc/thread-self/stat").read_text(encoding="utf-8")
    host_pid = int(raw.split(" ", 1)[0])
    identity = ProcessIdentity.capture(host_pid)
    state_fd = os.open(spec.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    _filesystem(spec, Path(spec_path).parent / "root")
    child = os.fork()  # windows-footgun: ok — module rejects non-Linux; namespace PID1 must fork its contained worker.
    if child == 0:
        os.close(state_fd)
        os.close(ready_fd)
        _parent_death(1)
        _restrictions(spec)
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/workspace", "TMPDIR": "/tmp", **spec.environment}
        environment["HERMES_WORKER_BROKER_FD"] = str(broker_fd)
        os.set_inheritable(broker_fd, True)
        os.execve(spec.command[0], spec.command, environment)
    os.close(broker_fd)
    # PID1 is never model-controlled. Its death tears down the entire PID namespace.
    os.write(ready_fd, json.dumps({"state": "contained", "namespace_init": identity.__dict__}).encode())
    os.close(ready_fd)
    while True:
        pid, status = os.waitpid(-1, 0)
        if pid == child:
            code = os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
            # No descendant may race the trusted export or retain a broker FD.
            try:
                os.kill(-1, signal.SIGKILL)  # windows-footgun: ok — Linux namespace PID1 reaps all descendants before export.
            except ProcessLookupError:
                pass
            while True:
                try:
                    os.waitpid(-1, 0)
                except ChildProcessError:
                    break
            if code == 0:
                _export(spec, state_fd)
            os.close(state_fd)
            os._exit(code)


def _supervise(spec_path: str, broker_fd: int, ready_fd: int):
    spec = _spec(spec_path)
    spec.validate()
    if not lease_valid(spec):
        raise RuntimeError("authority lease absent before containment")
    run = Path(spec_path).parent
    # Some portable Python builds omit os.pidfd_open despite a supporting kernel.
    pidfd_open = LIBC.pidfd_open
    pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    controller_fd = pidfd_open(spec.controller.pid, 0)
    if controller_fd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open")
    if not spec.controller.alive():
        raise RuntimeError("controller identity changed")
    child_read, child_write = os.pipe()
    guardian_pid = os.getpid()
    child = subprocess.Popen([
        "/usr/bin/unshare", "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", "--kill-child=KILL",
        sys.executable, "-I", str(Path(__file__).resolve()), "namespace", spec_path, str(broker_fd), str(child_write),
    ], pass_fds=(broker_fd, child_write), preexec_fn=lambda: _parent_death(guardian_pid),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    os.close(child_write)
    broker_monitor = select.poll()
    broker_monitor.register(broker_fd, select.POLLHUP | select.POLLERR | select.POLLRDHUP)
    state = {"permit_id": spec.permit_id, "scope_sha256": spec.scope_sha256,
             "controller": spec.controller.__dict__, "guardian": ProcessIdentity.capture(os.getpid()).__dict__,
             "deadline_monotonic": spec.deadline_monotonic, "state": "starting",
             "effects_reconciled": False, "exit_verified": False}
    state.update(artifacts_exported=False, artifacts_path=None, artifacts_manifest_sha256=None,
                 workspace_bytes=spec.workspace_bytes, workspace_inodes=spec.workspace_inodes)
    _write_json(run / "receipt.json", state)
    reason = "setup_failed"
    namespace = None
    try:
        readable, _, _ = select.select([child_read], [], [], min(5, max(0.01, spec.deadline_monotonic - time.monotonic())))
        raw = os.read(child_read, 65536) if readable else b""
        if not raw:
            raise RuntimeError("namespace startup failed")
        ready = json.loads(raw)
        namespace = ProcessIdentity(**ready["namespace_init"])
        # The namespace's sole initial child is the native worker. Capture its
        # host start identity before the controller can issue capabilities.
        children_path = Path(f"/proc/{namespace.pid}/task/{namespace.pid}/children")
        try:
            children = children_path.read_text(encoding="utf-8").split()
            if len(children) == 1:
                ready["worker"] = ProcessIdentity.capture(int(children[0])).__dict__
        except (OSError, ValueError):
            pass  # a worker that already exited cannot complete startup proof
        state.update(ready)
        _write_json(run / "receipt.json", state)
        os.write(ready_fd, raw)
        os.close(ready_fd)
        ready_fd = -1
        reason = "worker_exit"
        while child.poll() is None:
            checks = (
                (time.monotonic() >= spec.deadline_monotonic, "deadline"),
                (bool(select.select([controller_fd], [], [], 0)[0]), "controller_exit"),
                (bool(broker_monitor.poll(0)), "broker_exit"),
                ((run / "stop").exists(), "stop_requested"),
                (not lease_valid(spec), "authority_lease_revoked"),
            )
            failed = next((label for failed, label in checks if failed), None)
            if failed:
                reason = failed
                break
            select.select([controller_fd], [], [], min(0.05, max(0.001, spec.deadline_monotonic - time.monotonic())))
    finally:
        state.update(state="revoked", reason=reason, revoked_monotonic=time.monotonic())
        # A blocked/full receipt filesystem must never postpone containment.
        # Killing namespace PID1 also closes every worker broker descriptor;
        # in-flight remote effects retain their separate durable reservation.
        if child.poll() is None:
            child.kill()
        os.close(broker_fd)
        _write_json(run / "receipt.json", state)
        child.wait(timeout=5)
        # The launcher reap plus dead namespace init is kernel evidence that its
        # contained tasks exited. It says nothing about an accepted remote request.
        deadline = time.monotonic() + 3
        while namespace and namespace.alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        state.update(state="exited" if namespace and not namespace.alive() else "unresolved",
                     exit_verified=bool(namespace and not namespace.alive()), returncode=child.returncode,
                     ended_monotonic=time.monotonic())
        if reason == "worker_exit" and child.returncode == 0 and state["exit_verified"]:
            export = json.loads((Path(spec.state_dir) / "artifacts-receipt.json").read_text(encoding="utf-8"))
            if export.get("artifacts_manifest_sha256") != _guard_module.digest(export.get("artifacts_manifest")):
                raise RuntimeError("artifact export manifest mismatch")
            state.update(export)
        _write_json(run / "receipt.json", state)
        os.close(child_read)
        os.close(controller_fd)
        if ready_fd >= 0:
            os.close(ready_fd)


if __name__ == "__main__":
    modes = {"supervise": _supervise, "namespace": _namespace}
    modes[sys.argv[1]](sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
