#!/usr/bin/env python3
import unittest
from pathlib import Path
import yaml

ARCHON=Path(__file__).resolve().parents[2]

class BugfixGraphOrderTest(unittest.TestCase):
 def test_experiment_and_reconcile_precede_final_critic(self):
  w=yaml.safe_load((ARCHON/'workflows/bugfix.yaml').read_text())
  nodes={n['id']:n for n in w['nodes']}
  self.assertEqual(nodes['experiment-design']['depends_on'],['chain-gate'])
  self.assertEqual(nodes['evidence-seal']['depends_on'],['probe-run'])
  self.assertEqual(nodes['chain-verify']['depends_on'],['evidence-seal'])
  self.assertIn('evidence-provenance.py" verify', nodes['proof-manifest-gate']['bash'])
  self.assertEqual(nodes['experiment-run']['depends_on'],['experiment-design'])
  self.assertEqual(nodes['experiment-gate']['depends_on'],['experiment-run'])
  self.assertEqual(nodes['proof-reconcile']['depends_on'],['experiment-gate'])
  self.assertEqual(nodes['proof-manifest-gate']['depends_on'],['proof-reconcile'])
  self.assertEqual(nodes['rca-plan-loop']['depends_on'],['proof-manifest-gate'])
  self.assertEqual(nodes['approval-manifest-gate']['depends_on'],['rca-plan-shape'])
  self.assertEqual(nodes['rca-render']['depends_on'],['approval-manifest-gate'])
  self.assertEqual(nodes['post-approval-integrity']['depends_on'],['rca-approval'])
  self.assertEqual(nodes['bind-repo']['depends_on'],['post-approval-integrity'])
  self.assertNotIn('bash -c',nodes['experiment-run']['bash'])
  self.assertIn('experiment-runner.py',nodes['experiment-run']['bash'])

 def test_bugfix_workflows_never_read_floating_origin_main(self):
  canonical=(ARCHON/'workflows/bugfix.yaml').read_text()
  lite='\n'.join(p.read_text() for p in (ARCHON/'setup/lite/bugfix').glob('*') if p.is_file())
  self.assertNotIn('origin/main', canonical)
  self.assertNotIn('origin/main', lite)
  self.assertIn('baseline.commits', canonical)

 def test_human_gate_cannot_bypass_current_manifests(self):
  w=yaml.safe_load((ARCHON/'workflows/bugfix.yaml').read_text())
  nodes={n['id']:n for n in w['nodes']}
  approval=nodes['rca-approval']['approval']
  self.assertNotIn('edit fix-plan', approval['message'].lower())
  self.assertIn('Do not revise any diagnosis', approval['on_reject']['prompt'])
  post=nodes['post-approval-integrity']['bash']
  self.assertIn('validate-current-manifest', post)
  self.assertIn('controller-attest.py', post)

if __name__=='__main__':unittest.main()
