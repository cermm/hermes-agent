"""Independent actual CLI Ctrl-C at shutdown preserves completed evidence and reaps peers."""
import json,os,signal,subprocess,sys,time
from pathlib import Path
import psutil
import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX permissions and SIGINT")
REPO = Path(__file__).resolve().parents[3]
PEER=r'''
import json,os,time
from pathlib import Path
from _mock_lsp_server import read_message,write_message
marker=Path(os.environ['REVIEW_SHUTDOWN_MARKER'])
while message:=read_message():
 method=message.get('method');params=message.get('params') or {}
 if method=='initialize':
  write_message({'jsonrpc':'2.0','id':message['id'],'result':{'capabilities':{'textDocumentSync':1}}})
 elif method in ('textDocument/didOpen','textDocument/didChange'):
  doc=params['textDocument'];write_message({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics','params':{'uri':doc['uri'],'version':doc['version'],'diagnostics':[]}})
 elif method=='textDocument/diagnostic':
  write_message({'jsonrpc':'2.0','id':message['id'],'error':{'code':-32601,'message':'push only'}})
 elif method=='shutdown':
  marker.write_text(json.dumps({'pid':os.getpid(),'at':time.monotonic()}))
  while True:time.sleep(.05)
 elif method=='exit':break
'''

def test_cli_interrupt_during_cleanup_preserves_completed_json(tmp_path):
 project=tmp_path/'project';profile=tmp_path/'profile';profile.mkdir()
 subprocess.run(['git','init','-q',str(project)],check=True)
 source=project/'checked.py';source.write_text('value = 1\n')
 marker=tmp_path/'shutdown.json';peer=tmp_path/'peer.py'
 peer.write_text('#!'+sys.executable+'\nimport sys\nsys.path.insert(0, '+repr(str(REPO/'tests/agent/lsp'))+')\n'+PEER)
 peer.chmod(0o755)
 (profile/'config.yaml').write_text(json.dumps({'lsp':{'enabled':True,'install_strategy':'manual','wait_timeout':5,'servers':{'pyright':{'command':[str(peer)],'env':{'REVIEW_SHUTDOWN_MARKER':str(marker)}}}}}))
 env=dict(os.environ,HERMES_HOME=str(profile),PYTHONPATH=str(REPO))
 proc=subprocess.Popen([sys.executable,'-m','hermes_cli.main','lsp','check','--json',str(source)],cwd=project,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 child=None;result={}
 try:
  deadline=time.monotonic()+15
  while not marker.exists() and time.monotonic()<deadline:
   assert proc.poll() is None,proc.communicate()
   time.sleep(.01)
  assert marker.exists(),'peer did not reach shutdown'
  identity=json.loads(marker.read_text());child=psutil.Process(identity['pid']);created=child.create_time()
  proc.send_signal(signal.SIGINT)
  stdout,stderr=proc.communicate(timeout=12)
  result={'exit_code':proc.returncode,'stdout':stdout,'stderr':stderr,'peer_pid':child.pid,'peer_created':created,'peer_live':child.is_running() and child.status()!=psutil.STATUS_ZOMBIE}
  print('Independent shutdown-interrupt receipt:',json.dumps(result))
  assert proc.returncode==130,result
  output=json.loads(stdout)
  assert output['exit_code']==130 and output['files'][0]['status']=='fresh',output
  assert output['files'][0]['total']['count']==0
  assert not result['peer_live'],result
  assert source.read_text()=='value = 1\n'
 finally:
  if proc.poll() is None:proc.kill();proc.communicate()
  if child is not None and child.is_running() and child.status()!=psutil.STATUS_ZOMBIE:child.kill();child.wait(timeout=5)


def test_actual_cli_unreadable_parent_reports_bounded_invalid_input(tmp_path):
 project=tmp_path/'project';profile=tmp_path/'profile';profile.mkdir()
 subprocess.run(['git','init','-q',str(project)],check=True)
 (profile/'config.yaml').write_text(json.dumps({'lsp':{'enabled':False}}))
 sealed=project/'sealed';sealed.mkdir();source=sealed/'source.py';source.write_text('value = 1\n')
 sealed.chmod(0)
 try:
  result=subprocess.run([sys.executable,'-m','hermes_cli.main','lsp','check','--json',str(source)],cwd=project,env=dict(os.environ,HERMES_HOME=str(profile),PYTHONPATH=str(REPO)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=15)
  print('Independent unreadable-parent receipt:',json.dumps({'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr}))
  assert result.returncode==2
  output=json.loads(result.stdout)
  assert output['files'][0]['reason']=='unreadable_file'
  assert output['files'][0]['total'] is None
 finally:sealed.chmod(0o700)
