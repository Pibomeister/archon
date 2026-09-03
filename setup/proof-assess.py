#!/usr/bin/env python3
"""Typed, symptom-preserving proof transitions for the bugfix graph."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path


def load(ad, name):
    with open(ad / name, encoding='utf-8') as f: return json.load(f)

def write(ad, name, value):
    p=ad/name; t=p.with_suffix('.tmp'); t.write_text(json.dumps(value,indent=2)+'\n'); os.replace(t,p)

def symptom_ids(ad):
    try: return [x['id'] for x in load(ad,'symptoms.json')['effective_symptoms']]
    except Exception: return []

def chain(ad):
    cv=load(ad,'chain-verify.json'); comp=cv.get('comparison') or {}; verdict=comp.get('verdict')
    if verdict not in {'agree','conflict','cannot_determine'}: raise ValueError(f'chain verdict out of enum: {verdict}')
    doc={'schema_version':2,'verdict':verdict,'conflict_links':[x.get('index') for x in comp.get('links',[]) if x.get('verdict')=='conflict'],'active_symptom_ids':symptom_ids(ad)}
    write(ad,'chain-assessment.json',doc); print(f'CHAIN_ASSESS=OK verdict={verdict}')

def experiment(ad):
    e=load(ad,'experiment.json')
    if e.get('skipped'):
        doc={'schema_version':2,'verdict':'skipped','observed_hypothesis_id':None,'rca_hypothesis_id':None}
    else:
        result=load(ad,'experiment-result.json')
        text=json.dumps(result,sort_keys=True)
        hs=e.get('hypotheses') or []
        matched=[h['id'] for h in hs if h.get('signature') and h['signature'] in text]
        if result.get('result',{}).get('status') in {'timeout','degraded'}:
            verdict='degraded'; winner=None
        elif len(matched)!=1:
            verdict='ambiguous'; winner=None
        else:
            winner=matched[0]; verdict='confirm' if winner==e.get('rca_hypothesis_id') else 'conflict'
        doc={'schema_version':2,'verdict':verdict,'observed_hypothesis_id':winner,'rca_hypothesis_id':e.get('rca_hypothesis_id')}
    write(ad,'experiment-assessment.json',doc); print(f"EXPERIMENT_ASSESS=OK verdict={doc['verdict']}")

def reconcile(ad):
    c=load(ad,'chain-assessment.json'); e=load(ad,'experiment-assessment.json')
    active=c.get('active_symptom_ids') or symptom_ids(ad)
    try: gather_more=load(ad,'debug-phase.json').get('reproduction_status')=='gather-more'
    except (OSError,KeyError,json.JSONDecodeError): gather_more=False
    if c['verdict']=='conflict' or e['verdict']=='conflict':
        state='RECOVERY_SUCCESSOR_REQUIRED'; reason='contradicted-hypothesis'
    elif gather_more:
        state='EVIDENCE_BLOCKED'; reason='reproduction-or-occurrence-evidence-required'
    elif e['verdict'] in {'ambiguous','degraded'}:
        state='EVIDENCE_BLOCKED'; reason='experiment-unsettled'
    else:
        state='CONVERGED'; reason='current-proof-consistent'
    doc={'schema_version':2,'state':state,'reason':reason,'active_symptom_ids':active,'chain_verdict':c['verdict'],'experiment_verdict':e['verdict']}
    write(ad,'proof-recovery.json',doc)
    if state!='CONVERGED':
        print(f'PROOF_RECOVERY={state} reason={reason} active_symptoms={",".join(active)}'); return 1
    print('PROOF_RECOVERY=CONVERGED'); return 0

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('action',choices=['chain','experiment','reconcile']); ap.add_argument('artifacts',type=Path); a=ap.parse_args()
    try:
        if a.action=='chain': chain(a.artifacts)
        elif a.action=='experiment': experiment(a.artifacts)
        else: raise SystemExit(reconcile(a.artifacts))
    except (OSError,ValueError,KeyError,json.JSONDecodeError) as ex:
        print(f'PROOF_ASSESS=FAIL {ex}'); raise SystemExit(1)
if __name__=='__main__': main()
