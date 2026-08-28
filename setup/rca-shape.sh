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
if not (fp.get("approach") and fp.get("fix_site")):
    fail("fix-plan.json missing approach/fix_site")
alts = fp.get("alternatives")
if not (isinstance(alts, list) and (alts or fp.get("approach"))):
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
