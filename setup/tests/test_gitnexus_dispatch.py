import hashlib,hmac,json,os,stat,subprocess,tempfile,unittest
from pathlib import Path
SCRIPT=Path(__file__).resolve().parent.parent/'gitnexus-mcp-dispatch.py'
class DispatchTest(unittest.TestCase):
 def test_check_binds_real_index_to_commit(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td); subprocess.run(['git','init','-q',str(p)],check=True); subprocess.run(['git','-C',str(p),'config','user.email','x@y'],check=True); subprocess.run(['git','-C',str(p),'config','user.name','x'],check=True)
   (p/'.gitnexus').mkdir(); (p/'.gitnexus/run.cjs').write_text('')
   runner=p/'runner.js'; runner.write_text('')
   (p/'.gitnexus/meta.json').write_text(json.dumps({'runnerIdentity':{'invokedArtifact':{'path':str(runner),'digest':'sha256:'+hashlib.sha256(runner.read_bytes()).hexdigest()}}}))
   subprocess.run(['git','-C',str(p),'add','.'],check=True); subprocess.run(['git','-C',str(p),'commit','-qm','x'],check=True)
   sha=subprocess.check_output(['git','-C',str(p),'rev-parse','HEAD'],text=True).strip(); env=dict(os.environ,ARCHON_GITNEXUS_INDEX=str(p),ARCHON_GITNEXUS_COMMIT=sha)
   r=subprocess.run(['python3',str(SCRIPT),'--check'],env=env,capture_output=True,text=True); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
   env['ARCHON_GITNEXUS_COMMIT']='0'*40; r=subprocess.run(['python3',str(SCRIPT),'--check'],env=env,capture_output=True,text=True); self.assertNotEqual(r.returncode,0); self.assertIn('commit mismatch',r.stderr)

 def test_check_validates_chain_pinned_index(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); idx=root/'idx'; subprocess.run(['git','init','-q',str(idx)],check=True); subprocess.run(['git','-C',str(idx),'config','user.email','x@y'],check=True); subprocess.run(['git','-C',str(idx),'config','user.name','x'],check=True)
   (idx/'.gitnexus').mkdir(); (idx/'.gitnexus/run.cjs').write_text('')
   runner=idx/'runner.js'; runner.write_text('')
   (idx/'.gitnexus/meta.json').write_text(json.dumps({'runnerIdentity':{'invokedArtifact':{'path':str(runner),'digest':'sha256:'+hashlib.sha256(runner.read_bytes()).hexdigest()}}}))
   subprocess.run(['git','-C',str(idx),'add','.'],check=True); subprocess.run(['git','-C',str(idx),'commit','-qm','x'],check=True)
   sha=subprocess.check_output(['git','-C',str(idx),'rev-parse','HEAD'],text=True).strip()
   state={'logical_chain_id':'c'*32,'chain_secret':'secret-secret-secret-secret-secret-secret','baseline':{'gitnexus':{'index_path':str(idx),'commit':sha}},'state_mac':''}
   payload={k:v for k,v in state.items() if k!='state_mac'}; state['state_mac']=hmac.new(state['chain_secret'].encode(),json.dumps(payload,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
   sp=root/'chain.json'; sp.write_text(json.dumps(state)); sp.chmod(0o600)
   env=dict(os.environ,ARCHON_GITNEXUS_INDEX=str(idx),ARCHON_GITNEXUS_COMMIT=sha,ARCHON_BUGFIX_CHAIN_ID='c'*32,ARCHON_BUGFIX_CHAIN_STATE=str(sp))
   r=subprocess.run(['python3',str(SCRIPT),'--check'],env=env,capture_output=True,text=True); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
   state['baseline']['gitnexus']['commit']='0'*40; sp.write_text(json.dumps(state)); sp.chmod(0o600)
   r=subprocess.run(['python3',str(SCRIPT),'--check'],env=env,capture_output=True,text=True); self.assertNotEqual(r.returncode,0); self.assertIn('MAC mismatch',r.stderr)
if __name__=='__main__':unittest.main()
