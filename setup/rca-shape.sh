#!/usr/bin/env bash
# RCA artifact shape/consistency checks, factored out of bugfix.yaml's
# rca-gate node so U2's rca-converge can re-run them on every ACCEPT round.
# Chain-citation and hypotheses.json checks are NOT included (those stay
# rca-gate's job, run once against the frozen chain); this covers only the
# mutable-planning-artifact checks rca-gate runs from repo.json onward:
# repo enum, failing-test fields/enum/signature/integration_note, fix-plan
# approach/fix_site/alternatives, residuals, verify.json,
# files-allowlist normalization + test_file membership — plus two checks not
# in the original gate: fix-plan.files subset-of-allowlist, and the
# failing-test/repo cross-check restated explicitly (it was implicit there).
# probe.json is validated by setup/probe-shape.py, called from here — probe-run
# now executes those statements BEFORE this script runs, so the check has two
# callers and cannot live inline in either.
# On success this is a true drop-in for the mutable-artifact half of
# rca-gate: it (re)writes repo.txt = "<repo>\n" idempotently (9 downstream
# nodes `cat` it) and re-emits the RCA_NOTE=integration mutex line for
# kind=integration.
# Usage: rca-shape.sh <artifacts-dir> [caller-token]
# caller-token (default RCA_SHAPE) stamps the persisted stop with the calling
# node's identity -- RCA_GATE or RCA_PLAN_SHAPE -- which RUNBOOK.md routes on.
# stdout is unchanged either way: callers still sed RCA_SHAPE= themselves.
set -euo pipefail
AD="${1:?usage: rca-shape.sh <artifacts-dir> [caller-token]}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# A typed stop is the operator's only routing signal, but a failing node's
# stdout does not survive the workflow executor's failure path -- it reports a
# fragment of the script source instead, so every RCA_SHAPE=FAIL/
# RCA_INVESTIGATION_REQUIRED reason is destroyed at the failure boundary.
# Persist each typed line here, at the one chokepoint every caller shares,
# so terminal supervision can read the discriminator back off the run dir.
# Never let the surfacing path itself become a silent stop: if the run dir is
# unwritable, degrade to stdout rather than aborting before any typed message.
GATE_STATUS="$AD/gate-status.txt"
{ : > "$GATE_STATUS"; } 2>/dev/null || true
# Each caller renames this script's token to its own node-specific form
# (RCA_GATE=FAIL, RCA_PLAN_SHAPE=FAIL) and RUNBOOK.md gives those DIFFERENT
# remediations, so the persisted line has to carry the caller's identity or an
# operator cannot tell which call site stopped.
TOKEN="${2:-RCA_SHAPE}"
# Records typed STOPS only, one line, never a traceback. The truncate above
# clears a previous round's stop, so a run that passes this check and dies later
# has an empty file and no discriminator rather than a stale, misrouting one.
record() {
  local line
  line="$(printf '%s' "$1" | tr '\n' ' ')"
  { printf '%s\n' "${line/#RCA_SHAPE/$TOKEN}" >> "$GATE_STATUS"; } 2>/dev/null || true
}
emit() { printf '%s\n' "$1"; record "$1"; }

# V2 bugfix contract: the immutable source/effective symptom ledger, exact
# disposition/coverage bijections, lineage, occurrence proof, and closure
# classification are deterministic gates rather than RCA prose.
python3 "$HERE/bugfix-contract.py" normalize-gather-more "$AD" \
  || { emit "RCA_SHAPE=FAIL gather-more normalization"; exit 1; }
CONTRACT_OUT="$(python3 "$HERE/bugfix-contract.py" validate-causal-coverage --artifacts "$AD" 2>&1)" || {
  RETYPED="$(printf '%s\n' "$CONTRACT_OUT" | sed -E 's/^BUGFIX_(COVERAGE|CONTRACT)=FAIL/RCA_SHAPE=FAIL/')"
  printf '%s\n' "$RETYPED"
  # Persist exactly one typed line. A crash inside the contract produces a
  # traceback with no typed token; record a typed stop for it rather than
  # letting "AttributeError: ..." become the routing signal.
  TYPED="$(printf '%s\n' "$RETYPED" | grep -m1 '^RCA_SHAPE=FAIL' || true)"
  record "${TYPED:-RCA_SHAPE=FAIL contract crashed without a typed line (see run log)}"
  exit 1
}
CLASSIFY_OUT="$(python3 "$HERE/bugfix-contract.py" classify "$AD" 2>&1)" || {
  printf '%s\n' "$CLASSIFY_OUT" | sed -E 's/^BUGFIX_(COVERAGE|CONTRACT)=FAIL/RCA_SHAPE=FAIL/'
  CTYPED="$(printf '%s\n' "$CLASSIFY_OUT" | sed -E 's/^BUGFIX_(COVERAGE|CONTRACT)=FAIL/RCA_SHAPE=FAIL/' | grep -m1 '^RCA_SHAPE=FAIL' || true)"
  record "${CTYPED:-RCA_SHAPE=FAIL classification crashed without a typed line (see run log)}"
  exit 1
}

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
  # probe-run executes before rca-gate, so a stop here can say whether retrieval
  # was attempted and what it returned. An unanswered probe names the capability
  # that failed rather than blaming the analysis for not knowing.
  PROBE_STATE="$(python3 - "$AD" <<'PY'
import os, re, sys
path = os.path.join(sys.argv[1], "probe-results.txt")
if not os.path.isfile(path):
    print("not-run")
    raise SystemExit
head = open(path, encoding="utf-8", errors="replace").readline().strip()
match = re.match(r"PROBE_RUN=(SKIP|DEGRADED)\b", head)
print(match.group(1).lower() if match else "answered")
PY
)"
  if [ "$PROBE_STATE" != answered ] && [ "$INVESTIGATION_REASON" = reproduction-or-causal-proof-missing ]; then
    INVESTIGATION_REASON="occurrence-retrieval-unavailable"
  fi
  emit "RCA_INVESTIGATION_REQUIRED reason=$INVESTIGATION_REASON probes=$PROBE_STATE ticket=open no_implementation=true"
  exit 1
fi

# probe.json is validated by the SAME script probe-run uses before it executes
# these statements against production. One contract, two callers.
PROBE_OUT="$(python3 "$HERE/probe-shape.py" "$AD" --token "RCA_SHAPE=FAIL" 2>&1)" \
  || { printf '%s\n' "$PROBE_OUT"
       # Persist exactly one typed line, the same way the contract output above
       # is handled: probe-shape can emit a traceback with no typed token, and a
       # traceback must never become the routing signal.
       TYPED="$(printf '%s\n' "$PROBE_OUT" | grep -m1 '^RCA_SHAPE=FAIL' || true)"
       record "${TYPED:-RCA_SHAPE=FAIL probe shape crashed without a typed line (see run log)}"
       exit 1; }

RCA_SHAPE_TOKEN="$TOKEN" python3 - "$AD" <<'PY'
import json, os, re, sys

ad = sys.argv[1]


def load(name):
    return json.load(open(os.path.join(ad, name), encoding="utf-8"))


TOKEN = os.environ.get("RCA_SHAPE_TOKEN", "RCA_SHAPE")


def emit(line):
    print(line)
    # One line, carrying the caller's token: gate_discriminator reads the last
    # line, so a reason wrapped over several lines would lose its typed prefix.
    flat = " ".join(line.split())
    if TOKEN != "RCA_SHAPE" and flat.startswith("RCA_SHAPE"):
        flat = TOKEN + flat[len("RCA_SHAPE"):]
    try:
        with open(os.path.join(ad, "gate-status.txt"), "a", encoding="utf-8") as fh:
            fh.write(flat + "\n")
    except OSError:
        pass  # stdout is still the primary channel; never mask a typed stop


def fail(msg):
    emit(f"RCA_SHAPE=FAIL {msg}")
    sys.exit(1)


try:
    repo = load("repo.json")["repo"]
except Exception as e:
    fail(f"repo.json missing or malformed: {e}")

if repo not in ("api", "web-app", "both"):
    fail(f"repo out of enum: {repo}")

# repo names the repository THIS CHAIN CHANGES, not the ticket's surface area.
# A symptom dispositioned separate-ticket / by-design / product-semantics
# becomes a split ticket and never widens repo; scoping repo over the whole
# report turns any bug with a cross-repo symptom into a spurious
# CROSS_REPO_BUG, stopping a run whose fix was single-repo all along.
FIX_SHAPED = {"fixed", "class-hardening-only"}
try:
    dispositions = load("symptom-dispositions.json")["dispositions"]
except Exception as e:
    fail(f"symptom-dispositions.json missing or malformed: {e}")
fix_shaped = [d for d in dispositions if d.get("disposition") in FIX_SHAPED]
# repo is contract-required on separate-ticket rows already; require it here too,
# or a fix-shaped row that simply omits it silently defeats the cross-check below.
missing_repo = [d.get("symptom_id") for d in fix_shaped if d.get("repo") not in ("api", "web-app")]
if missing_repo:
    fail(f"REPO_SCOPE fix-shaped dispositions missing repo: {sorted(missing_repo)}")
fix_repos = {d["repo"] for d in fix_shaped}
# Only the unambiguous contradiction is a gate: repo names one repository while
# the symptoms this chain fixes live in a different one. repo="both" is left to
# CROSS_REPO_BUG below — a single symptom whose one fix legitimately spans repos
# cannot express that through per-symptom dispositions, so rejecting "both"
# mechanically would block a legitimate run. The prompt carries that rule.
if fix_repos and repo != "both" and fix_repos != {repo}:
    fail(f"REPO_SCOPE repo={repo} but fix-shaped symptoms are in {sorted(fix_repos)}")

if repo == "both":
    fail("CROSS_REPO_BUG (v1 is single-repo)")

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
print(f"RCA_SHAPE=OK repo={repo} kind={ft['kind']}")  # success is not a stop: never persisted
PY
