import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def invoke(tmp_path, argv, *, source='from hermes_cli.main import main\nmain()\n', tty=False, extra_env=None):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    consumer = tmp_path / 'consumer'
    consumer.mkdir(exist_ok=True)
    reader, writer = os.pipe()
    environment = dict(os.environ, HOME=str(home), HERMES_HOME=str(home / '.hermes'), PYTHONPATH=str(ROOT), META_FD=str(writer))
    environment.pop('HERMES_KANBAN_TASK', None)
    environment.update(extra_env or {})
    args = [str(writer) if arg == '{fd}' else arg for arg in argv]
    if tty:
        import pty
        master, slave = pty.openpty()
    else:
        master, slave = -1, -1
    try:
        result = subprocess.run([sys.executable, '-c', source, *args], cwd=consumer, env=environment, pass_fds=(writer,), stdin=slave if tty else subprocess.DEVNULL, stdout=slave if tty else subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        os.close(writer)
        writer = -1
        frames = os.read(reader, 2048)
        assert os.read(reader, 1) == b''
        return result, frames
    finally:
        for fd in (reader, writer, master, slave):
            if fd >= 0:
                os.close(fd)


@pytest.mark.linux_only
@pytest.mark.parametrize('post_validation_failure', [False, True])
def test_query_file_retains_same_metadata_ownership_as_inline_query(tmp_path, post_validation_failure):
    query = tmp_path / 'query.txt'
    query.write_text('private offline query fixture')
    outputs = []
    source = 'from hermes_cli.main import main\nmain()\n'
    if post_validation_failure:
        source = '''import hermes_cli.main as entry
def fail_startup(args):
    raise RuntimeError('offline post-validation startup failure')
entry._prepare_agent_startup = fail_startup
entry.main()
'''
    for query_args in (['--query', query.read_text()], ['--query-file', str(query)]):
        result, frames = invoke(tmp_path, ['--cli', 'chat', '--continue', '__missing_metadata_review_session__', *query_args, '--quiet', '--result-meta-fd', '{fd}'], source=source)
        outputs.append((result.returncode, frames))
    assert outputs[0][0] == 0 and json.loads(outputs[0][1])['failed'] is True
    assert outputs[1] == outputs[0]


@pytest.mark.linux_only
def test_real_tty_metadata_runs_once_and_closes_pipe(tmp_path):
    observed = tmp_path / 'observed.json'
    source = '''
import json, os, sys, types
from pathlib import Path
import cli
from hermes_cli.cli_result_metadata import CLIResultMetadataMixin
from hermes_cli.result_metadata import claim_result_metadata_fd
assert sys.stdin.isatty() and sys.stdout.isatty()
assert cli._should_seed_interactive('hello', None, False, False)
events = []
class Query(CLIResultMetadataMixin):
    max_turns = 10
    console = types.SimpleNamespace(print=lambda *a, **k: None)
    def _claim_active_session(self, *a, **k): return True
    def _show_security_advisories(self): pass
    def _print_exit_summary(self, **k): events.append('summary')
    def run(self): raise AssertionError('metadata query entered interactive REPL')
    def chat(self, *a, **k):
        events.append('query')
        self._publish_result_metadata(dict(completed=True, failed=False, partial=False, interrupted=False, api_calls=1))
query = Query()
query.result_meta_fd = claim_result_metadata_fd(int(os.environ['META_FD']))
cli._finalize_single_query = lambda q: events.append('finalize')
cli._run_single_query_mode(query, 'hello', None, False, False)
Path(os.environ['OBSERVED']).write_text(json.dumps(events))
'''
    result, frames = invoke(tmp_path, [], source=source, tty=True, extra_env={'OBSERVED': str(observed)})
    assert result.returncode == 0, result.stderr
    assert json.loads(frames)['completed'] is True
    assert observed.read_text() == '["query", "summary", "finalize"]'


@pytest.mark.linux_only
def test_kanban_signal_shutdown_publishes_owned_interruption_frame(tmp_path):
    source = '''
import os, signal, types
import cli
from hermes_cli.cli_result_metadata import CLIResultMetadataMixin
from hermes_cli.result_metadata import claim_result_metadata_fd
class Query(CLIResultMetadataMixin):
    max_turns = 10
    agent = None
query = Query()
query.result_meta_fd = claim_result_metadata_fd(int(os.environ['META_FD']))
os.environ['HERMES_KANBAN_TASK'] = 'offline-metadata-signal-fixture'
cli._install_single_query_signal_handlers(query)
os.kill(os.getpid(), signal.SIGTERM)
raise AssertionError('signal failed to terminate')
'''
    result, frames = invoke(tmp_path, [], source=source)
    assert result.returncode == 0, result.stderr
    assert frames, 'owned metadata pipe closed without terminal frame'
    assert json.loads(frames)['interrupted'] is True


@pytest.mark.linux_only
@pytest.mark.parametrize('contents', [None, ''])
def test_invalid_query_file_is_rejected_without_success_frame(tmp_path, contents):
    query = tmp_path / 'query.txt'
    if contents is not None:
        query.write_text(contents)
    result, frames = invoke(tmp_path, ['--cli', 'chat', '--query-file', str(query), '--result-meta-fd', '{fd}'])
    assert result.returncode == 2
    assert frames == b''


@pytest.mark.linux_only
@pytest.mark.parametrize('backpressure', [False, True])
def test_kanban_signal_metadata_failure_still_hard_exits(tmp_path, backpressure):
    source = '''
import os, signal
import cli
from hermes_cli.result_metadata import claim_result_metadata_fd
reader, writer = os.pipe()
if os.environ['BACKPRESSURE'] == '1':
    os.set_blocking(writer, False)
    try:
        while True:
            os.write(writer, b'x' * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(writer, True)
else:
    os.close(reader)
class Query:
    agent = None
query = Query()
query.result_meta_fd = claim_result_metadata_fd(writer)
os.environ['HERMES_KANBAN_TASK'] = 'offline-metadata-signal-fixture'
cli._install_single_query_signal_handlers(query)
os.kill(os.getpid(), signal.SIGTERM)
raise AssertionError('shutdown returned')
'''
    environment = dict(os.environ, HERMES_HOME=str(tmp_path / 'hermes'),
                       PYTHONPATH=str(ROOT), BACKPRESSURE='1' if backpressure else '0')
    result = subprocess.run([sys.executable, '-c', source], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=12)
    assert result.returncode == 1
