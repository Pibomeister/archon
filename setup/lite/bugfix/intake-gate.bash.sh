set -euo pipefail
B="$ARTIFACTS_DIR/bug-report-normalized.md"
test -s "$B" || { echo "INTAKE_GATE=FAIL no bug-report-normalized.md"; exit 1; }
grep -q '<trace-context>' "$B" || { echo "INTAKE_GATE=FAIL missing trace-context open"; exit 1; }
grep -q '</trace-context>' "$B" || { echo "INTAKE_GATE=FAIL missing trace-context close"; exit 1; }
python3 - "$ARTIFACTS_DIR/evidence-plan.json" <<'PY' || { echo "INTAKE_GATE=FAIL evidence-plan shape"; exit 1; }
import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
for k in ("identifiers", "error_strings", "sentry_refs", "linear_refs"):
    assert isinstance(p.get(k), list), f"{k} must be a list"
assert p.get("repo_hint") in ("api", "web-app", "unknown"), "repo_hint out of enum"
for ident in p["identifiers"]:
    assert ident.get("resolution") in ("given", "ambiguous"), "identifier without resolution"
    assert ident.get("kind") and str(ident.get("value", "")).strip(), "identifier missing kind/value"
for k in ("repro_command", "repro_observed"):
    assert k in p and (p[k] is None or isinstance(p[k], str)), f"{k} must be a string or null"
tw = p.get("time_window")
assert tw is None or (isinstance(tw, dict) and tw.get("start") and tw.get("end")), "time_window malformed"
PY
if [ -n "${ARCHON_BUGFIX_CONTINUATION_SEED:-}" ]; then
  python3 /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/archon-run.py \
    import-continuation --artifacts "$ARTIFACTS_DIR" --finalize-ledger \
    || { echo "INTAKE_GATE=FAIL continuation ledger import"; exit 1; }
fi
python3 /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/bugfix-contract.py seal-ledger "$ARTIFACTS_DIR" \
  || { echo "INTAKE_GATE=FAIL symptom ledger"; exit 1; }
python3 /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/bugfix-contract.py bind-chain-ledger "$ARTIFACTS_DIR" \
  || { echo "INTAKE_GATE=FAIL symptom ledger chain binding"; exit 1; }
python3 - "$ARTIFACTS_DIR/triage.json" <<'PY' || { echo "INTAKE_GATE=FAIL triage shape"; exit 1; }
import json, sys
t = json.load(open(sys.argv[1], encoding="utf-8"))
assert t.get("size") in ("S", "M", "L"), "size out of enum"
for k in ("reasons", "hot_path_hits", "unknowns"):
    assert isinstance(t.get(k), list), f"{k} must be a list"
PY
echo "INTAKE_GATE=PASS"
