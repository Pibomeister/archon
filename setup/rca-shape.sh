#!/usr/bin/env bash
# RCA artifact shape/consistency checks, factored out of bugfix.yaml's
# rca-gate node so U2's rca-converge can re-run them on every ACCEPT round.
# Chain-citation and hypotheses.json checks are NOT included (those stay
# rca-gate's job, run once against the frozen chain); this covers only the
# mutable-planning-artifact checks rca-gate runs from repo.json onward:
# repo enum, failing-test fields/enum/signature/integration_note, fix-plan
# approach/fix_site/alternatives, probe.json validator, residuals, verify.json,
# files-allowlist normalization + test_file membership — plus two checks not
# in the original gate: fix-plan.files subset-of-allowlist, and the
# failing-test/repo cross-check restated explicitly (it was implicit there).
# On success this is a true drop-in for the mutable-artifact half of
# rca-gate: it (re)writes repo.txt = "<repo>\n" idempotently (9 downstream
# nodes `cat` it) and re-emits the RCA_NOTE=integration mutex line for
# kind=integration.
# Usage: rca-shape.sh <artifacts-dir>
set -euo pipefail
AD="${1:?usage: rca-shape.sh <artifacts-dir>}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# V2 bugfix contract: the immutable source/effective symptom ledger, exact
# disposition/coverage bijections, lineage, occurrence proof, and closure
# classification are deterministic gates rather than RCA prose.
python3 "$HERE/bugfix-contract.py" normalize-gather-more "$AD" \
  || { echo "RCA_SHAPE=FAIL gather-more normalization"; exit 1; }
CONTRACT_OUT="$(python3 "$HERE/bugfix-contract.py" validate-causal-coverage --artifacts "$AD" 2>&1)" || {
  printf '%s\n' "$CONTRACT_OUT" | sed -E 's/^BUGFIX_(COVERAGE|CONTRACT)=FAIL/RCA_SHAPE=FAIL/'
  exit 1
}
python3 "$HERE/bugfix-contract.py" classify "$AD" >/dev/null \
  || { echo "RCA_SHAPE=FAIL classification"; exit 1; }

# A thin report may truthfully end investigation without an implementation
# plan. That is valid open work, but it must stop with a typed evidence request
# rather than masquerading as malformed JSON or flowing into RED/fix nodes.
INVESTIGATION_REASON="$(python3 - "$AD" <<'PY'
import json, os, sys
ad = sys.argv[1]
load = lambda name: json.load(open(os.path.join(ad, name), encoding="utf-8"))
plan = load("fix-plan.json")
if plan.get("approach"):
    print("")
elif load("boundary-trace.json").get("surface_equivalence", {}).get("reported_surface_status") == "ambiguous":
    print("surface-ambiguous")
else:
    print("reproduction-or-causal-proof-missing")
PY
)"
if [ -n "$INVESTIGATION_REASON" ]; then
  echo "RCA_INVESTIGATION_REQUIRED reason=$INVESTIGATION_REASON ticket=open no_implementation=true"
  exit 1
fi

python3 - "$AD" <<'PY'
import json, os, re, sys

ad = sys.argv[1]


def load(name):
    return json.load(open(os.path.join(ad, name), encoding="utf-8"))


def fail(msg):
    print(f"RCA_SHAPE=FAIL {msg}")
    sys.exit(1)


try:
    repo = load("repo.json")["repo"]
except Exception as e:
    fail(f"repo.json missing or malformed: {e}")

if repo == "both":
    fail("CROSS_REPO_BUG (v1 is single-repo)")
if repo not in ("api", "web-app"):
    fail(f"repo out of enum: {repo}")

try:
    ft = load("failing-test.json")
except Exception as e:
    fail(f"failing-test.json missing or malformed: {e}")
for k in ("repo", "kind", "test_file", "test_name", "command", "predicted_failure_signature"):
    if not ft.get(k):
        fail(f"failing-test.json missing {k}")
if ft["repo"] != repo:
    fail("failing-test repo != repo.json repo")
if ft["kind"] not in ("unit", "integration", "vitest", "playwright"):
    fail("kind out of enum")
sig = ft["predicted_failure_signature"]
if len(sig) < 10:
    fail("signature too generic: under 10 chars")
if sig.strip() in {"Error", "error", "failed", "FAIL", "undefined"}:
    fail("signature too generic")
if ft["kind"] == "integration":
    if not ft.get("integration_note"):
        fail("kind=integration requires integration_note")
    print("RCA_NOTE=integration mutex: the repro will own ports 54322/8001 machine-globally")

try:
    fp = load("fix-plan.json")
except Exception as e:
    fail(f"fix-plan.json missing or malformed: {e}")
debug = load("debug-phase.json")
gather_more = debug.get("reproduction_status") == "gather-more"
if gather_more:
    if fp.get("approach") or fp.get("fix_site") or fp.get("files"):
        fail("gather-more fix plan must remain blocked")
elif not (fp.get("approach") and fp.get("fix_site")):
    fail("fix-plan.json missing approach/fix_site")
alts = fp.get("alternatives")
if not (isinstance(alts, list) and (gather_more or alts or fp.get("approach"))):
    fail("fix-plan.json missing alternatives (list; may hold a 'none' entry)")

try:
    pr = load("probe.json")
except Exception as e:
    fail(f"probe.json missing or malformed: {e}")
probes = pr.get("probes")
if not (isinstance(probes, list) and len(probes) <= 3):
    fail("probe.json probes must be a list of at most 3")
if not (probes or pr.get("none_reason")):
    fail("probe.json: empty probes requires none_reason")
for pb in probes:
    if not (pb.get("id") and pb.get("question") and pb.get("sql")):
        fail("probe entry missing id/question/sql")
    sql = pb["sql"].strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", sql):
        fail(f"probe {pb['id']}: must start with SELECT/WITH")
    if re.search(
        r"(?i)\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum)\b",
        sql,
    ):
        fail(f"probe {pb['id']}: write/DDL keyword rejected")
    if ";" in sql:
        fail(f"probe {pb['id']}: single statement only")
    limits = [int(x) for x in re.findall(r"(?i)\blimit\s+(\d+)\b", sql)]
    aggregate_only = bool(re.search(r"(?i)\b(count|sum|avg|min|max)\s*\(", sql))
    if not aggregate_only and not limits:
        fail(f"probe {pb['id']}: row-returning query requires LIMIT <= 100")
    if limits and max(limits) > 100:
        fail(f"probe {pb['id']}: LIMIT exceeds 100")

try:
    res = load("residuals.json")["residuals"]
except Exception as e:
    fail(f"residuals.json missing or malformed: {e}")
if not (isinstance(res, list) and res):
    fail("residuals.json empty (every reported symptom needs a disposition)")
for r in res:
    if not r.get("symptom"):
        fail("residual missing symptom")
    d = r.get("disposition")
    if d not in ("fixed-by-this-chain", "by-design", "separate-bug"):
        fail(f"residual disposition out of enum: {d}")
    if not r.get("citation"):
        fail(f"residual '{r['symptom'][:40]}' missing citation")
    if d == "separate-bug":
        if r.get("repo") not in ("api", "web-app"):
            fail("separate-bug residual missing repo")
        if not r.get("ticket_stub"):
            fail("separate-bug residual missing ticket_stub")

try:
    pats = load("verify.json")["test_patterns"]
except Exception as e:
    fail(f"verify.json missing or malformed: {e}")
if not (isinstance(pats, list) and pats and all(isinstance(p, str) and p.strip() for p in pats)):
    fail("verify.json empty")
non_unit = [p for p in pats if p.endswith((".int.spec.ts", ".e2e.spec.ts", ".ai.spec.ts", ".ext.spec.ts"))]
if non_unit:
    fail(f"verify.json test_patterns must be unit specs: {non_unit}")

try:
    allow = load("files-allowlist.json")
except Exception as e:
    fail(f"files-allowlist.json missing or malformed: {e}")
if isinstance(allow, dict) and isinstance(allow.get("files"), list):
    # Normalize the {"files": [...]} synonym shape to the canonical bare
    # array — downstream check-scope.py consumes the bare list.
    allow = allow["files"]
    json.dump(allow, open(os.path.join(ad, "files-allowlist.json"), "w", encoding="utf-8"), indent=2)
if not (isinstance(allow, list) and allow):
    fail("files-allowlist.json empty")
if ft["test_file"] not in allow:
    fail("files-allowlist must include test_file")

fp_files = fp.get("files")
if fp_files:
    missing = [f for f in fp_files if f not in allow]
    if missing:
        fail(f"fix-plan.files not subset of files-allowlist: {missing}")

open(os.path.join(ad, "repo.txt"), "w", encoding="utf-8").write(repo + "\n")
print(f"RCA_SHAPE=OK repo={repo} kind={ft['kind']}")
PY
