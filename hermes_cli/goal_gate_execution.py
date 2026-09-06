"""Approval and process ownership for deferred gateway quality gates."""

import subprocess
import time

from hermes_cli._subprocess_compat import IS_WINDOWS, kill_process_tree, windows_hide_flags
from tools.interrupt import is_interrupted


def _text(value) -> str:
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else (value or '')


def run_deferred_goal_gate(command: str, *, timeout: int, cwd: str | None, tail_chars: int) -> tuple[bool, int, str]:
    """Run an explicitly authorized gate, reaping its own process tree on cancellation."""
    from tools.approval import check_all_command_guards

    if is_interrupted():
        return False, -1, '[gate interrupted before execution]'
    try:
        decision = check_all_command_guards(command, 'local', require_explicit_authorization=True)
    except Exception as exc:
        return False, -1, f'[gate approval failed: {type(exc).__name__}]'
    if not decision.get('approved'):
        return False, -1, str(decision.get('message') or 'BLOCKED: Gate requires session approval')[-tail_chars:]
    if is_interrupted():
        return False, -1, '[gate interrupted before execution]'

    options = {'creationflags': windows_hide_flags()} if IS_WINDOWS else {'process_group': 0}
    try:
        proc = subprocess.Popen(command, shell=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='replace', cwd=cwd or None, **options)
    except Exception as exc:
        return False, -1, f'[gate could not run: {type(exc).__name__}: {exc}]'
    deadline = time.monotonic() + max(1, int(timeout))
    stdout = stderr = ''
    reason = ''
    try:
        while True:
            if is_interrupted():
                reason = 'gate interrupted'
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = f'gate timed out after {timeout}s'
                break
            try:
                stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                if is_interrupted():
                    reason = 'gate interrupted'
                break
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = _text(exc.stdout), _text(exc.stderr)
    except BaseException:
        kill_process_tree(proc)
        raise
    finally:
        if reason:
            kill_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=1)
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = _text(exc.stdout), _text(exc.stderr)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
    combined = _text(stdout) + ('\n' + _text(stderr) if stderr else '')
    if reason:
        return False, -1, (combined + f'\n[{reason}]')[-tail_chars:]
    return proc.returncode == 0, proc.returncode, combined[-tail_chars:]
