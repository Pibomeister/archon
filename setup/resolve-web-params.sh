#!/usr/bin/env bash
# Web-lane parameter derivation from an API handoff.
# Usage: resolve-web-params.sh <goodword-root> <absolute-api-handoff.json> <artifacts-dir> [api-port-base] [web-port-base]
# The port bases are the lane's own literals; port-alloc.sh turns each into a
# per-run port so two runs of this lane cannot bind the same server, and so a
# full-sdlc-web run cannot land on a full-sdlc-api run's port (which used to be
# literally the same 4123, with both smoke checks passing against either server).
set -euo pipefail
ROOT="${1:?usage: resolve-web-params.sh <root> <handoff> <artifacts-dir> [api-port-base] [web-port-base]}"
HANDOFF="${2-}"
AD="${3:?usage: resolve-web-params.sh <root> <handoff> <artifacts-dir> [api-port-base] [web-port-base]}"
APIBASE="${4-}"
WEBBASE="${5-}"
test -n "$HANDOFF" || { echo "WEB_PARAMS=FAIL no API handoff path in run message"; exit 1; }
case "$HANDOFF" in /*) : ;; *) echo "WEB_PARAMS=FAIL API handoff path must be absolute, got: $HANDOFF"; exit 1 ;; esac
test -f "$HANDOFF" || { echo "WEB_PARAMS=FAIL API handoff missing: $HANDOFF"; exit 1; }
PROVIDER="${ARCHON_FEATURE_PROVIDER-}"
LANE="${ARCHON_FEATURE_LANE-}"
CHAIN_ID="${ARCHON_FEATURE_CHAIN_ID-}"
test -n "$PROVIDER" || { echo "WEB_PARAMS=FAIL missing ARCHON_FEATURE_PROVIDER"; exit 1; }
test -n "$LANE" || { echo "WEB_PARAMS=FAIL missing ARCHON_FEATURE_LANE"; exit 1; }
test -n "$CHAIN_ID" || { echo "WEB_PARAMS=FAIL missing ARCHON_FEATURE_CHAIN_ID"; exit 1; }
python3 "$ROOT/.archon/setup/archon-run.py" verify-feature-handoff   --provider "$PROVIDER" --lane "$LANE" --artifacts "$AD" "$HANDOFF"
APIPORT=""; WEBPORT=""
[ -n "$APIBASE" ] && { APIPORT=$(bash "$ROOT/.archon/setup/port-alloc.sh" "$APIBASE" "$(basename "$HANDOFF")") || { echo "WEB_PARAMS=FAIL cannot allocate api port from base $APIBASE"; exit 1; }; }
[ -n "$WEBBASE" ] && { WEBPORT=$(bash "$ROOT/.archon/setup/port-alloc.sh" "$WEBBASE" "$(basename "$HANDOFF")") || { echo "WEB_PARAMS=FAIL cannot allocate web port from base $WEBBASE"; exit 1; }; }
python3 - "$ROOT" "$HANDOFF" "$AD" "$APIPORT" "$WEBPORT" <<'PY'
import hashlib, json, re, subprocess, sys
from pathlib import Path
root, handoff, ad = map(Path, sys.argv[1:4])
api_port, web_port = sys.argv[4], sys.argv[5]
data = json.loads(handoff.read_text(encoding="utf-8"))
body = {k: v for k, v in data.items() if k not in {"handoff_sha256", "handoff_mac"}}
canon = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
if hashlib.sha256(canon).hexdigest() != data.get("handoff_sha256"):
    raise SystemExit("WEB_PARAMS=FAIL API handoff SHA mismatch")
if data.get("kind") != "archon-feature-api-handoff" or data.get("schema_version") != 1:
    raise SystemExit("WEB_PARAMS=FAIL unsupported API handoff schema")
spec = Path(str(data.get("spec", "")))
if not spec.is_absolute() or not spec.is_file():
    raise SystemExit("WEB_PARAMS=FAIL handoff spec missing")
if hashlib.sha256(spec.read_bytes()).hexdigest() != data.get("spec_sha256"):
    raise SystemExit("WEB_PARAMS=FAIL handoff spec hash mismatch")
apiwt = Path(str(data.get("api_worktree", "")))
api_artifacts = Path(str(data.get("api_artifacts", "")))
try:
    apiwt.resolve().relative_to((root / "api" / ".worktrees").resolve())
except ValueError:
    raise SystemExit("WEB_PARAMS=FAIL API worktree is outside api/.worktrees")
if not (apiwt / ".git").exists():
    raise SystemExit("WEB_PARAMS=FAIL API worktree missing")
if not api_artifacts.is_absolute() or not api_artifacts.is_dir():
    raise SystemExit("WEB_PARAMS=FAIL API artifacts missing")
web_allowlist = api_artifacts / "web-files-allowlist.json"
if not web_allowlist.is_file():
    raise SystemExit("WEB_PARAMS=FAIL missing approved web-files-allowlist.json")
actual_web_allowlist_sha = hashlib.sha256(web_allowlist.read_bytes()).hexdigest()
if actual_web_allowlist_sha != data.get("web_files_allowlist_sha256"):
    raise SystemExit("WEB_PARAMS=FAIL approved web allowlist hash mismatch")
web_files = json.loads(web_allowlist.read_text(encoding="utf-8"))
if not isinstance(web_files, list) or not web_files or not all(isinstance(x, str) and x.strip() for x in web_files):
    raise SystemExit("WEB_PARAMS=FAIL approved web-files-allowlist.json is empty or malformed")
head = subprocess.run(["git", "-C", str(apiwt), "rev-parse", "HEAD"], capture_output=True, encoding="utf-8")
if head.returncode != 0 or head.stdout.strip() != data.get("api_head_sha"):
    raise SystemExit("WEB_PARAMS=FAIL API handoff head changed")
slug = re.sub(r"[^a-z0-9]+", "-", spec.stem.lower()).strip("-")[:55]
if not slug:
    raise SystemExit("WEB_PARAMS=FAIL empty slug")
params = {
    "spec": str(spec),
    "slug": slug,
    "branch": f"archon/{slug}-web",
    "worktree": str(root / "web-app" / ".worktrees" / f"{slug}-web"),
    "handoff": str(handoff),
    "api_worktree": str(apiwt),
    "api_branch": data.get("api_branch"),
    "api_head_sha": data.get("api_head_sha"),
    "api_pr_url": data.get("api_pr_url"),
    "api_run_id": data.get("api_run_id"),
    "logical_chain_id": data.get("logical_chain_id"),
    "shared_plan_sha256": data.get("shared_plan_sha256"),
    "web_files_allowlist_sha256": data.get("web_files_allowlist_sha256"),
}
if api_port:
    params["api_port"] = int(api_port)
if web_port:
    params["web_port"] = int(web_port)
ad.mkdir(parents=True, exist_ok=True)
(ad / "params.json").write_text(json.dumps(params, indent=2) + "\n", encoding="utf-8")
(ad / "api-handoff.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
# The web lane inherits approved API-planning artifacts byte-for-byte. The
# implementation node may use this allowlist, but cannot author or broaden it.
(ad / "files-allowlist.json").write_bytes(web_allowlist.read_bytes())
(ad / "web-files-allowlist.json").write_bytes(web_allowlist.read_bytes())
if not (ad / "premises.json").exists():
    (ad / "premises.json").write_text("[]\n", encoding="utf-8")
if not (ad / "reader-audit.json").exists():
    (ad / "reader-audit.json").write_text('{"columns": []}\n', encoding="utf-8")
criteria = []
text = spec.read_text(encoding="utf-8", errors="replace")
for match in re.finditer(r"(?ims)^##\s+(Browser evidence|Acceptance criteria|Verification)\s*$\n(.*?)(?=^##\s+|\Z)", text):
    for line in match.group(2).splitlines():
        item = re.sub(r"^\s*[-*0-9.)]+\s*", "", line).strip()
        if item:
            criteria.append(item[:240])
if not criteria:
    criteria = ["Open the changed same-origin application path and verify the feature behavior described by the spec is visible or operable."]
(ad / "browser-evidence.json").write_text(json.dumps({"required": [{"id": f"browser-{i+1}", "criterion": c} for i, c in enumerate(criteria[:12])]}, indent=2) + "\n", encoding="utf-8")
print(f"WEB_PARAMS=OK spec={spec} slug={slug} api_run={data.get('api_run_id')} api_port={api_port or 'none'} web_port={web_port or 'none'}")
PY
