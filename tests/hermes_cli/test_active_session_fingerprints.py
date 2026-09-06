"""Fork process-identity regressions retained alongside current lease semantics."""
import os
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
from hermes_cli import active_sessions


def test_fingerprint_unknown_preserves_strict_coordination(monkeypatch):
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: True)
    monkeypatch.setattr(active_sessions, '_process_identity', lambda pid: None)
    # A readable legacy timestamp cannot replace a stored host fingerprint.
    monkeypatch.setattr(active_sessions, '_process_start_time', lambda pid: 1000.0)
    entry = {'pid': 123, 'process_start_time': 1000.0, 'process_identity': 42}
    assert active_sessions._pid_liveness(123, 1000.0, 42) is None
    assert active_sessions._prune_dead([entry]) == [entry]
    with pytest.raises(active_sessions.ActiveSessionRegistryError, match='liveness is unknown'):
        active_sessions._prune_dead([entry], strict=True)
    with pytest.raises(active_sessions.ActiveSessionRegistryError, match='liveness is unknown'):
        active_sessions._prune_dead([{**entry, 'track_liveness': True}])


def test_changed_fingerprint_prunes_even_when_legacy_float_matches(monkeypatch):
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: True)
    monkeypatch.setattr(active_sessions, '_process_identity', lambda pid: 43)
    monkeypatch.setattr(active_sessions, '_process_start_time', lambda pid: 1000.0)
    entry = {'pid': 123, 'process_start_time': 1000.0, 'process_identity': 42}
    assert active_sessions._prune_dead([entry], strict=True) == []


@pytest.mark.parametrize('identity', [True, 'bad', -1, [], {}, 4.2])
def test_malformed_fingerprint_does_not_erase_an_owner(tmp_path, identity):
    path = tmp_path / 'active_sessions.json'
    active_sessions._write_entries(path, [{'lease_id': 'lease', 'session_id': 'session', 'pid': os.getpid(), 'process_identity': identity}])
    before = path.read_bytes()
    with pytest.raises(active_sessions.ActiveSessionRegistryError, match='invalid process identity'):
        active_sessions._read_entries(path, strict=True)
    assert path.read_bytes() == before

def test_pid_alive_uses_safe_pid_exists_without_signalling(monkeypatch):
    checked: list[int] = []

    monkeypatch.setattr(
        active_sessions.os,
        "kill",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("os.kill used")),
    )
    monkeypatch.setattr(
        "gateway.status._pid_exists",
        lambda pid: checked.append(int(pid)) or True,
    )

    assert active_sessions._pid_alive(12345) is True
    assert checked == [12345]

@pytest.mark.linux_only
def test_pid_alive_legacy_start_time_uses_independent_clock_tick_boundary(monkeypatch):
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)
    monkeypatch.setattr(active_sessions, "_process_start_time", lambda _pid: 1000.0)
    monkeypatch.setattr(os, "sysconf", lambda name: 100)

    assert active_sessions._pid_alive(12345, 1000.005) is True
    assert active_sessions._pid_alive(12345, 1000.011) is False


def test_legacy_timestamp_fallback_without_platform_clock_tick(monkeypatch):
    monkeypatch.delattr(os, 'sysconf', raising=False)
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: True)
    monkeypatch.setattr(active_sessions, '_process_start_time', lambda pid: 1000.0)
    assert active_sessions._process_start_time_tolerance() == 0.001
    assert active_sessions._pid_alive(12345, 1000.0005) is True
    assert active_sessions._pid_alive(12345, 1000.002) is False

def test_pid_alive_prefers_stable_process_identity(monkeypatch):
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)
    monkeypatch.setattr(active_sessions, "_process_start_time", lambda _pid: 1000.0)
    monkeypatch.setattr(active_sessions, "_process_identity", lambda _pid: 4242)

    assert active_sessions._pid_alive(12345, 9999.0, 4242) is True
    assert active_sessions._pid_alive(12345, 1000.0, 4243) is False
    assert active_sessions._pid_alive(12345, 1000.0, "not-an-int") is False

def test_acquired_active_session_records_stable_process_identity(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(active_sessions, "_process_identity", lambda _pid: 8675309)

    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-with-identity",
        surface="cli",
        config={"max_concurrent_sessions": 1},
    )

    assert message is None
    assert lease is not None
    try:
        snapshot = active_sessions.active_session_registry_snapshot()
        assert snapshot[0]["process_identity"] == 8675309
    finally:
        lease.release()

def test_active_session_hard_exit_is_reclaimed(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo_root)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os\n"
                "from hermes_cli.active_sessions import try_acquire_active_session\n"
                "lease, message = try_acquire_active_session("
                "session_id='crash-session', surface='cli', "
                "config={'max_concurrent_sessions': 1})\n"
                "assert message is None, message\n"
                "print(os.getpid(), flush=True)\n"
                "os._exit(0)\n"
            ),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    child_pid = int(child.stdout.strip())

    lease, message = active_sessions.try_acquire_active_session(
        session_id="next-session",
        surface="cli",
        config={"max_concurrent_sessions": 1},
    )

    assert child_pid > 0
    assert message is None
    assert lease is not None
    assert [entry["session_id"] for entry in active_sessions.active_session_registry_snapshot()] == [
        "next-session"
    ]
    lease.release()

def test_concurrent_acquire_claims_only_one_last_slot(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = {"max_concurrent_sessions": 1}

    def _claim(index: int):
        return active_sessions.try_acquire_active_session(
            session_id=f"session-{index}",
            surface="cli",
            config=cfg,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_claim, range(8)))

    leases = [lease for lease, message in results if lease is not None and message is None]
    blocked = [message for lease, message in results if lease is None and message]

    try:
        assert len(leases) == 1
        assert len(blocked) == 7
        assert active_sessions.active_session_registry_snapshot()[0]["session_id"].startswith("session-")
    finally:
        for lease in leases:
            lease.release()
