"""Dashboard process identity at discovery and stop boundaries."""

import signal
import subprocess
from unittest.mock import patch

import pytest

from hermes_cli.dashboard_procs import _scan_dashboard_processes, _kill_stale_dashboard_processes


@pytest.mark.linux_only
def test_tmux_launcher_is_never_selected_or_signalled():
    parent, child = 41001, 41002
    rows = (
        f'{parent} /usr/bin/tmux new-session -d -s dashboard-session "hermes dashboard --no-open"\n'
        f'{child} /opt/hermes/bin/hermes dashboard --no-open\n'
    )

    def ps_only(argv, **kwargs):
        assert argv == ["ps", "-A", "-o", "pid=,command="]
        return subprocess.CompletedProcess(argv, 0, stdout=rows)

    signals = []

    def record_signal(pid, sig):
        signals.append((pid, sig))

    with (
        patch("hermes_cli.dashboard_procs.subprocess.run", side_effect=ps_only),
        patch("hermes_cli.process_identity.ledger_entries", return_value=[]),
        patch("gateway.status._pid_exists", return_value=False),
        patch("gateway.status.get_process_start_time", return_value=100),
        patch("gateway.status._read_process_cmdline", return_value="/opt/hermes/bin/hermes dashboard --no-open"),
        patch("os.kill", side_effect=record_signal),
        patch("time.sleep"),
    ):
        assert _scan_dashboard_processes() == [(child, "/opt/hermes/bin/hermes dashboard --no-open")]
        result = _kill_stale_dashboard_processes(reason="explicit stop")

    assert result["matched"] == [child]
    assert signals == [(child, signal.SIGTERM)]


@pytest.mark.linux_only
def test_reused_dashboard_pid_is_not_signalled_even_with_same_command():
    pid = 42001
    command = "/opt/hermes/bin/hermes dashboard --no-open"
    rows = f"{pid} {command}\n"
    signals = []

    with (
        patch("hermes_cli.dashboard_procs.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=rows)),
        patch("hermes_cli.process_identity.ledger_entries", return_value=[]),
        patch("gateway.status.get_process_start_time", side_effect=[100, 101]),
        patch("gateway.status._read_process_cmdline", return_value=command),
        patch("os.kill", side_effect=lambda target, sig: signals.append((target, sig))),
        patch("time.sleep"),
    ):
        result = _kill_stale_dashboard_processes(reason="explicit stop")

    assert result["matched"] == [pid]
    assert result["killed"] == []
    assert result["failed"] and result["failed"][0][0] == pid
    assert signals == []


@pytest.mark.linux_only
@pytest.mark.parametrize("command,selected", [
    ("/opt/bin/hermes dashboard --no-open", True),
    ("python3 -m hermes_cli.main serve --port 9119", True),
    ("/opt/hermes/hermes_cli/main.py dashboard --no-open", True),
    ("python3 /opt/hermes/hermes_cli/main.py --profile bit serve", True),
    ("hermes -p bit dashboard", True),
    ("hermes --profile=bit serve", True),
    ("/usr/bin/tmux new-session -d 'hermes dashboard --no-open'", False),
    ("bash -c 'hermes serve --port 9119'", False),
    ("code --command 'hermes dashboard --no-open'", False),
    ("grep hermes dashboard", False),
    ("hermes --reasoning dashboard gateway run", False),
])
def test_only_real_hermes_entrypoints_match(command, selected):
    pid = 43001
    with (
        patch("hermes_cli.dashboard_procs.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=f"{pid} {command}\n")),
        patch("hermes_cli.process_identity.ledger_entries", return_value=[]),
    ):
        rows = _scan_dashboard_processes()
    assert [row[0] for row in rows] == ([pid] if selected else [])


@pytest.mark.linux_only
def test_spawn_ledger_and_desktop_exclusion_are_preserved():
    process_pid, ledger_pid = 44001, 44002
    with (
        patch("hermes_cli.dashboard_procs.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=f"{process_pid} hermes dashboard\n")),
        patch("hermes_cli.process_identity.ledger_entries", return_value=[
            {"pid": ledger_pid, "purpose": "serve", "argv": "hermes --profile bit serve"},
        ]),
    ):
        assert _scan_dashboard_processes(exclude_pids={process_pid}) == [
            (ledger_pid, "hermes --profile bit serve")
        ]
        assert _scan_dashboard_processes(exclude_pids={ledger_pid}) == [
            (process_pid, "hermes dashboard")
        ]


@pytest.mark.linux_only
def test_unverifiable_start_identity_never_signals():
    pid = 45001
    with (
        patch("hermes_cli.dashboard_procs.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=f"{pid} hermes dashboard\n")),
        patch("hermes_cli.process_identity.ledger_entries", return_value=[]),
        patch("gateway.status.get_process_start_time", return_value=None),
        patch("os.kill") as kill,
    ):
        result = _kill_stale_dashboard_processes(reason="explicit stop")
    assert result["matched"] == [pid]
    assert result["failed"] and result["killed"] == []
    kill.assert_not_called()


@pytest.mark.linux_only
def test_pid_reuse_before_force_escalation_never_gets_sigkill():
    pid = 46001
    sent = []
    state = {"start": 100}
    with (
        patch("hermes_cli.dashboard_procs.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=f"{pid} hermes dashboard\n")),
        patch("hermes_cli.process_identity.ledger_entries", return_value=[]),
        patch("gateway.status.get_process_start_time", side_effect=lambda target: state["start"]),
        patch("gateway.status._read_process_cmdline", return_value="hermes dashboard"),
        patch("gateway.status._pid_exists", return_value=True),
        patch("os.kill", side_effect=lambda target, sig: (sent.append((target, sig)), state.update(start=101))),
        patch("time.monotonic", side_effect=[0.0, 4.0]),
    ):
        result = _kill_stale_dashboard_processes(reason="explicit stop")
    assert sent == [(pid, signal.SIGTERM)]
    assert result["failed"] and result["killed"] == []


@pytest.mark.linux_only
@pytest.mark.parametrize("live_create,drift_during_ledger", [(100.0, False), (101.0, False), (None, False), (100.0, True)])
def test_wrapped_backend_requires_exact_readable_ledger_incarnation(tmp_path, live_create, drift_during_ledger):
    import json
    from types import SimpleNamespace

    import psutil
    from hermes_cli.process_identity import install_id

    pid = 47001
    ledger_path = tmp_path / "spawn-ledger.json"
    ledger_path.write_text(json.dumps([{
        "pid": pid, "purpose": "serve", "create_time": 100.0,
        "install": install_id(), "argv": "/opt/wrapper backend",
    }]))
    state = {"start": 100, "verifying": False}
    sent = []

    def create_time():
        if live_create is None:
            raise psutil.AccessDenied(pid)
        if drift_during_ledger and state["verifying"]:
            state["start"] = 101
        return live_create

    def read_command(target):
        state["verifying"] = True
        return "/opt/wrapper backend"

    with (
        patch("hermes_cli.dashboard_procs.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")),
        patch("hermes_cli.process_identity._ledger_path", return_value=ledger_path),
        patch("psutil.Process", return_value=SimpleNamespace(create_time=create_time)),
        patch("gateway.status.get_process_start_time", side_effect=lambda target: state["start"]),
        patch("gateway.status._read_process_cmdline", side_effect=read_command),
        patch("gateway.status._pid_exists", return_value=False),
        patch("os.kill", side_effect=lambda target, sig: sent.append((target, sig))),
        patch("time.sleep"),
    ):
        result = _kill_stale_dashboard_processes(reason="explicit stop")
    assert result["matched"] == [pid]
    if live_create == 100.0 and not drift_during_ledger:
        assert sent == [(pid, signal.SIGTERM)]
        assert result["killed"] == [pid]
    else:
        assert sent == []
        assert result["killed"] == []
        assert result["failed"] and result["failed"][0][0] == pid


@pytest.mark.linux_only
@pytest.mark.parametrize("replace_during", ["term", "kill"])
def test_replacement_during_argv_verification_is_not_signalled(replace_during):
    pid = 48001
    state = {"start": 100, "sent": []}

    def read_command(target):
        assert target == pid
        if replace_during == "term" or state["sent"]:
            state["start"] = 101
        return "hermes dashboard"

    with (
        patch("hermes_cli.dashboard_procs.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=f"{pid} hermes dashboard\n")),
        patch("hermes_cli.process_identity.ledger_entries", return_value=[]),
        patch("gateway.status.get_process_start_time", side_effect=lambda target: state["start"]),
        patch("gateway.status._read_process_cmdline", side_effect=read_command),
        patch("os.kill", side_effect=lambda target, sig: state["sent"].append((target, sig))),
        patch("time.monotonic", side_effect=[0.0, 4.0]),
    ):
        result = _kill_stale_dashboard_processes(reason="explicit stop")
    assert state["sent"] == ([] if replace_during == "term" else [(pid, signal.SIGTERM)])
    assert result["killed"] == []
    assert result["failed"] and result["failed"][0][0] == pid


@pytest.mark.linux_only
@pytest.mark.parametrize("tail", ["dashboard --no-open", "--profile bit serve --port 9119"])
def test_python_console_script_is_stopped_without_ledger(tail):
    pid = 49001
    command = f"/opt/venv/bin/python /opt/venv/bin/hermes {tail}"
    sent = []
    with (
        patch("hermes_cli.dashboard_procs.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=f"{pid} {command}\n")),
        patch("hermes_cli.process_identity.ledger_entries", return_value=[]),
        patch("gateway.status.get_process_start_time", return_value=100),
        patch("gateway.status._read_process_cmdline", return_value=command),
        patch("gateway.status._pid_exists", return_value=False),
        patch("os.kill", side_effect=lambda target, sig: sent.append((target, sig))),
        patch("time.sleep"),
    ):
        result = _kill_stale_dashboard_processes(reason="explicit stop")
    assert sent == [(pid, signal.SIGTERM)]
    assert result["killed"] == [pid]
