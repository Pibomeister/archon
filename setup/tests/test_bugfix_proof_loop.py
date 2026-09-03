#!/usr/bin/env python3
import json,subprocess,tempfile,unittest
from pathlib import Path
SCRIPT=Path(__file__).resolve().parent.parent/'proof-assess.py'

def write(ad,n,v):(ad/n).write_text(json.dumps(v))
class ProofTransitionTest(unittest.TestCase):
 def base(self,td,chain='agree',exp='confirm'):
  ad=Path(td); write(ad,'symptoms.json',{'effective_symptoms':[{'id':'E1'}]})
  write(ad,'chain-verify.json',{'independent_root_cause':'x','comparison':{'verdict':chain,'links':[]}})
  write(ad,'experiment.json',{'skipped':exp=='skipped','hypotheses':[{'id':'H1','signature':'OBSERVED=one'}],'rca_hypothesis_id':'H1'})
  if exp!='skipped': write(ad,'experiment-result.json',{'result':{'status':'exit','output':'OBSERVED=one' if exp=='confirm' else 'OBSERVED=two'}})
  return ad
 def call(self,a,ad):return subprocess.run(['python3',str(SCRIPT),a,str(ad)],capture_output=True,text=True)
 def test_conflict_requires_successor_and_preserves_symptom(self):
  with tempfile.TemporaryDirectory() as td:
   ad=self.base(td,chain='conflict'); self.assertEqual(self.call('chain',ad).returncode,0); self.assertEqual(self.call('experiment',ad).returncode,0)
   r=self.call('reconcile',ad); self.assertEqual(r.returncode,1); self.assertIn('RECOVERY_SUCCESSOR_REQUIRED',r.stdout); self.assertIn('E1',r.stdout)
 def test_consistent_proof_converges(self):
  with tempfile.TemporaryDirectory() as td:
   ad=self.base(td); self.call('chain',ad); self.call('experiment',ad); r=self.call('reconcile',ad); self.assertEqual(r.returncode,0,r.stdout+r.stderr); self.assertIn('CONVERGED',r.stdout)
 def test_ambiguous_experiment_blocks_evidence(self):
  with tempfile.TemporaryDirectory() as td:
   ad=self.base(td,exp='conflict'); self.call('chain',ad); self.call('experiment',ad); r=self.call('reconcile',ad); self.assertEqual(r.returncode,1); self.assertIn('EVIDENCE_BLOCKED',r.stdout)
 def test_gather_more_blocks_instead_of_converging_on_a_conditional_fix(self):
  with tempfile.TemporaryDirectory() as td:
   ad=self.base(td); write(ad,'debug-phase.json',{'reproduction_status':'gather-more'}); self.call('chain',ad); self.call('experiment',ad)
   r=self.call('reconcile',ad); self.assertEqual(r.returncode,1); self.assertIn('EVIDENCE_BLOCKED',r.stdout); self.assertIn('reproduction-or-occurrence',r.stdout)
if __name__=='__main__':unittest.main()
