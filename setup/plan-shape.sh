#!/usr/bin/env bash
# Plan artifact shape checks, factored out of full-sdlc-api.yaml's
# plan-snapshot node so plan-converge (U1's plan-loop) can re-run the same
# checks on every ACCEPT round without duplicating the logic. Verifies
# plan.md's five headings, verify.json/files-allowlist.json/web-files-allowlist.json/reader-audit.json,
# web-reader-audit.json, web-premises.json, browser-evidence.json/.sha256 shape, and — when the spec declares premises — that every premises.json
# evidence quote is cited verbatim in the worktree (same cited() helper as
# plan-snapshot, verbatim). When the spec pins an interface
# (`## Interface (pinned)`) and a joint-plan.json exists, every
# contracts[].artifact must appear verbatim in that section or be named by a
# `deviation: <artifact>` line in plan.md, and joint-plan.json must carry a
# non-empty pinned_decisions list of {symbol, file, rule}.
# Usage: plan-shape.sh <artifacts-dir> <worktree> <spec-path>
set -euo pipefail
AD="${1:?usage: plan-shape.sh <artifacts-dir> <worktree> <spec-path>}"
WT="${2:?usage: plan-shape.sh <artifacts-dir> <worktree> <spec-path>}"
SPEC="${3:?usage: plan-shape.sh <artifacts-dir> <worktree> <spec-path>}"

# Browser applicability comes from the run's repo, never from the policy file
# itself: a document that declares its own exemption is not a gate. Legacy params
# with no "repo" resolve to api, which is what such runs targeted.
HERE_PS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PS_REPO=$(python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding='utf-8')).get('repo') or 'api')
except Exception:
    print('api')
" "$AD/params.json")
PS_PROFILE=$(bash "$HERE_PS/repo-profile.sh" "$PS_REPO") || { echo "PLAN_SHAPE=FAIL repo-profile.sh failed for repo $PS_REPO"; exit 1; }
eval "$PS_PROFILE"
PS_HAS_BROWSER="$HAS_BROWSER"

test -s "$AD/plan.md" || { echo "PLAN_SHAPE=FAIL no plan.md"; exit 1; }
for h in "## Goal" "## Files" "## Approach" "## Test scenarios" "## Verification"; do
  grep -q "^$h" "$AD/plan.md" || { echo "PLAN_SHAPE=FAIL missing $h"; exit 1; }
done
if [ -f "$AD/params.json" ]; then
  python3 "$HERE_PS/validate-joint-plan.py" "$AD"
# No params.json fallback is possible here: this branch is reached only when
# params.json is absent, which is the very file the fallback would read. A
# repository-list run that resumes with the chain env dropped AND no params.json
# passes this check silently; params.json is written before any plan node runs,
# so that combination means the controller never got as far as writing it.
elif [ "${ARCHON_FEATURE_SCOPE-}" = repositories ]; then
  echo "PLAN_SHAPE=FAIL repository feature run missing params.json"; exit 1
fi
python3 -c "import json,sys; p=json.load(open(sys.argv[1]))['test_patterns']; assert isinstance(p,list) and p and all(isinstance(x,str) and x.strip() for x in p)" "$AD/verify.json" || { echo "PLAN_SHAPE=FAIL verify.json missing or empty"; exit 1; }
python3 -c "import json,sys; a=json.load(open(sys.argv[1])); assert isinstance(a,list) and a and all(isinstance(x,str) and x.strip() for x in a)" "$AD/files-allowlist.json" || { echo "PLAN_SHAPE=FAIL files-allowlist.json missing or empty"; exit 1; }
python3 -c "import json,sys; a=json.load(open(sys.argv[1])); assert isinstance(a,list) and all(isinstance(x,str) and x.strip() for x in a)" "$AD/web-files-allowlist.json" || { echo "PLAN_SHAPE=FAIL web-files-allowlist.json missing or malformed"; exit 1; }
python3 -c "import json,sys; c=json.load(open(sys.argv[1]))['columns']; assert isinstance(c,list)" "$AD/reader-audit.json" || { echo "PLAN_SHAPE=FAIL reader-audit.json missing or malformed"; exit 1; }
python3 -c "import json,sys; c=json.load(open(sys.argv[1]))['columns']; assert isinstance(c,list)" "$AD/web-reader-audit.json" 2>/dev/null || { echo "PLAN_SHAPE=FAIL web-reader-audit.json missing or malformed"; exit 1; }
python3 - "$AD/browser-evidence.json" "$AD/browser-evidence.sha256" "$PS_HAS_BROWSER" <<'PY' 2>/dev/null || { echo "PLAN_SHAPE=FAIL browser-evidence.json/.sha256 missing or malformed (or a not_applicable disposition from a repo that HAS a browser surface)"; exit 1; }
import hashlib, json, re, sys
policy_path, digest_path, has_browser = sys.argv[1], sys.argv[2], sys.argv[3]
policy = json.load(open(policy_path, encoding="utf-8"))
required = policy.get("required")
# A repository whose changes are not reachable through a browser cannot write an
# honest policy, and an empty "required" would otherwise be indistinguishable
# from forgetting to write one. The typed disposition is the ONLY form in which
# "required" may be empty, and it is hashed below exactly like a populated
# policy -- so the claim is frozen at approval, not something a later node can
# quietly widen. Anything else with an empty list still fails.
not_applicable = policy.get("not_applicable")
if isinstance(not_applicable, str) and not_applicable.strip():
    # Gated on the REPO, not on the file's own say-so. Without this the api and
    # web-app gates could be switched off by adding one string to the artifact.
    assert not has_browser, (
        "not_applicable is only valid for a repo with no browser surface")
    assert required == [], "not_applicable browser policy must carry required: []"
    required = []
else:
    assert isinstance(required, list) and required
seen = set()
for item in required:
    assert isinstance(item, dict)
    cid = item.get("id")
    assert isinstance(cid, str) and cid and cid not in seen
    seen.add(cid)
    assert isinstance(item.get("criterion"), str) and item["criterion"].strip()
    path = item.get("path")
    assert isinstance(path, str) and path.startswith("/") and not path.startswith("//")
    assertions = item.get("assertions")
    assert isinstance(assertions, list) and assertions
    for assertion in assertions:
        assert isinstance(assertion, dict)
        assert assertion.get("type") in {"text", "selector", "url", "title", "testid", "click", "fill"}
        assert isinstance(assertion.get("value"), str) and assertion["value"]
approved = open(digest_path, encoding="utf-8").read().strip().split()[0].lower()
actual = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
assert re.fullmatch(r"[0-9a-f]{64}", approved) and approved == actual
PY

if grep -q '^## Premises to verify' "$SPEC"; then
  python3 - "$AD/premises.json" "$WT" <<'PY' || { echo "PLAN_SHAPE=FAIL premises.json missing, empty, or uncited"; exit 1; }
import json, os, re, subprocess, sys
prem = json.load(open(sys.argv[1]))
assert isinstance(prem, list) and prem, "empty premises list though spec declares premises"
def cited(q, path):
    if subprocess.run(["grep", "-qF", q, path]).returncode == 0:
        return True
    # Verbatim modulo formatting: Prettier wraps statements across lines,
    # so a quote that is one line in the plan may be split in the source.
    # Collapse whitespace runs on both sides before the substring check.
    try:
        content = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return False
    return bool(content) and " ".join(q.split()) in " ".join(content.split())
for p in prem:
    ev = p.get("evidence") or []
    assert ev, f"premise {p.get('id')} has no evidence"
    ok = False
    for e in ev:
        # a "path:N" / "path:N-M" suffix is a formatting habit, not a different file
        f = os.path.join(sys.argv[2], re.sub(r":[0-9]+(?:-[0-9]+)?$", "", e["file"]))
        if os.path.isfile(f) and cited(e["quote"], f):
            ok = True
            break
    assert ok, f"premise {p.get('id')}: no evidence quote found verbatim in the worktree"
PY
fi

if [ -f "$AD/joint-plan.json" ] && grep -q '^## Interface (pinned)' "$SPEC"; then
  python3 - "$AD/joint-plan.json" "$SPEC" "$AD/plan.md" <<'PY'
import json, re, sys
joint_path, spec_path, plan_path = sys.argv[1:4]
joint = json.load(open(joint_path, encoding="utf-8"))
# A pinned interface is only pinned if the plan says what it pinned it to. The
# planner emits pinned_decisions: [{symbol, file, rule}], one per decision the
# consumer repo is now allowed to depend on; without it the section is a
# sentence in a spec that no later gate can check anything against.
decisions = joint.get("pinned_decisions")
if not isinstance(decisions, list) or not decisions or not all(
        isinstance(d, dict) and all(isinstance(d.get(f), str) and d.get(f).strip()
                                    for f in ("symbol", "file", "rule"))
        for d in decisions):
    print("PLAN_SHAPE=FAIL pinned_decisions missing")
    sys.exit(1)
contracts = joint.get("contracts") or []
spec_text = open(spec_path, encoding="utf-8").read()
m = re.search(r"^## Interface \(pinned\)\n(.*?)(?=^## |\Z)", spec_text, re.M | re.S)
section = m.group(1) if m else ""
deviations = {line[len("deviation:"):].strip()
              for line in open(plan_path, encoding="utf-8") if line.startswith("deviation:")}
for c in contracts:
    artifact = c.get("artifact")
    if artifact and artifact not in section and artifact not in deviations:
        print(f"PLAN_SHAPE=FAIL interface deviation undeclared: {artifact}")
        sys.exit(1)
PY
fi
echo "PLAN_SHAPE=OK"
