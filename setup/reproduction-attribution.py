#!/usr/bin/env python3
"""Reproduction is attribution, for symptoms that reproduce deterministically.

A `fixed` disposition requires the reported occurrence to be attributed to the
selected cause. For a data bug that means a production row, and probe-run plus
rca-reassess can supply it while the RCA is still open. For a deterministic
behaviour bug there is no row to find: the occurrence IS the behaviour, and the
thing that proves it is the RED test.

That proof arrives too late to be seen. The RCA writes symptom-dispositions.json
and debug-phase.json's reproduction_status before any test exists -- measured on
run 127a883f, 41 minutes before red-sha.txt -- so a locally reproducible bug is
permanently `class-hardening-only` and its ticket can never reach RESOLVED. The
asymmetry is the defect: retrieved evidence can upgrade a disposition, reproduced
evidence cannot.

This upgrades a symptom to `fixed` only when every one of these holds, and each
is read from an artifact another gate already attested:

  1. its causal-coverage row names a red_test;
  2. that row is counterfactual_user_visible -- the symptom is one a user sees;
  3. the committed RED test produced the predicted_failure_signature that was
     derived from the report (commit-red re-proves this after formatting);
  4. the fix turned it GREEN (green.json).

No model judgement is involved and nothing is inferred. A symptom that fails any
condition keeps the disposition the RCA gave it. Symptoms dispositioned
by-design, separate-ticket, product-semantics or unresolved are never touched:
this only ever moves class-hardening-only -> fixed.

Usage: reproduction-attribution.py --artifacts <dir> [--apply]
Without --apply it reports what it would do and changes nothing.
"""
import argparse
import json
import sys
from pathlib import Path


def load(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def eligible(ad: Path):
    """(upgradable_ids, reason_if_none)."""
    coverage = load(ad / "causal-coverage.json", {}) or {}
    dispositions = load(ad / "symptom-dispositions.json", {}) or {}
    failing = load(ad / "failing-test.json", {}) or {}
    rows = {r.get("symptom_id"): r for r in coverage.get("coverage") or [] if isinstance(r, dict)}
    if not rows:
        return [], "no causal-coverage rows"

    signature = str(failing.get("predicted_failure_signature") or "").strip()
    if not signature:
        return [], "no predicted_failure_signature"
    # commit-red re-runs the COMMITTED repro and greps for the signature; its
    # output file existing with the signature in it is that proof.
    committed = ad / "red-committed-out.txt"
    if not committed.is_file() or signature not in committed.read_text(encoding="utf-8", errors="replace"):
        return [], "committed repro did not reproduce the predicted signature"

    green = load(ad / "green.json", {}) or {}
    if green.get("green") is not True:
        # attempt-scoped fallback: fix-converge writes attempt-<n>/green.json
        attempts = sorted(ad.glob("attempt-*/green.json"))
        green = load(attempts[-1], {}) if attempts else {}
        if green.get("green") is not True:
            return [], "no GREEN result"

    out = []
    for row in dispositions.get("dispositions") or []:
        if not isinstance(row, dict) or row.get("disposition") != "class-hardening-only":
            continue
        cov = rows.get(row.get("symptom_id"))
        if not cov:
            continue
        if not str(cov.get("red_test") or "").strip():
            continue
        if cov.get("counterfactual_user_visible") is not True:
            continue
        out.append(row["symptom_id"])
    return out, "" if out else "no class-hardening-only symptom carries a user-visible red test"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    ad = args.artifacts

    ids, reason = eligible(ad)
    if not ids:
        print(f"REPRO_ATTRIBUTION=NOOP reason={reason}")
        return 0
    if not args.apply:
        print(f"REPRO_ATTRIBUTION=WOULD_UPGRADE symptoms={','.join(ids)}")
        return 0

    disp_path = ad / "symptom-dispositions.json"
    dispositions = load(disp_path, {})
    for row in dispositions.get("dispositions") or []:
        if row.get("symptom_id") in ids:
            row["disposition"] = "fixed"
            row["attributed_by"] = "reproduction"
    disp_path.write_text(json.dumps(dispositions, indent=2) + "\n", encoding="utf-8")

    cov_path = ad / "causal-coverage.json"
    coverage = load(cov_path, {})
    for row in coverage.get("coverage") or []:
        if row.get("symptom_id") in ids:
            row["occurrence_attributed"] = True
    cov_path.write_text(json.dumps(coverage, indent=2) + "\n", encoding="utf-8")

    phase_path = ad / "debug-phase.json"
    phase = load(phase_path, {})
    if phase:
        phase["reproduction_status"] = "reproduced"
        phase_path.write_text(json.dumps(phase, indent=2) + "\n", encoding="utf-8")

    print(f"REPRO_ATTRIBUTION=UPGRADED symptoms={','.join(ids)} "
          "(the committed RED test reproduced the reported signature and the fix turned it GREEN)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
