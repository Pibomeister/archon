#!/usr/bin/env bash
# Web-lane parameter derivation from an API handoff.
# Usage: resolve-web-params.sh <goodword-root> <absolute-api-handoff.json> <artifacts-dir> [api-port-base] [web-port-base]
# The port bases are the lane's own literals; port-alloc.sh turns each into a
# per-run port so two runs of this lane cannot bind the same server, and so a
# full-sdlc-web run cannot land on a full-sdlc-api run's port (which used to be
# literally the same 4123, with both smoke checks passing against either server).
set -euo pipefail
LAYER="${ARCHON_LAYER:-$(cd "$(dirname "$0")/.." && pwd)}"
ROOT="${1:?usage: resolve-web-params.sh <root> <handoff> <artifacts-dir> [api-port-base] [web-port-base]}"
HANDOFF="${2-}"
AD="${3:?usage: resolve-web-params.sh <root> <handoff> <artifacts-dir> [api-port-base] [web-port-base]}"
APIBASE="${4-}"
WEBBASE="${5-}"
PROVIDER="${ARCHON_FEATURE_PROVIDER-}"
LANE="${ARCHON_FEATURE_LANE-}"
CHAIN_ID="${ARCHON_FEATURE_CHAIN_ID-}"
FEATURE_SCOPE="${ARCHON_FEATURE_SCOPE-fullstack}"
test -n "$PROVIDER" || { echo "WEB_PARAMS=FAIL missing ARCHON_FEATURE_PROVIDER"; exit 1; }
test -n "$LANE" || { echo "WEB_PARAMS=FAIL missing ARCHON_FEATURE_LANE"; exit 1; }
test -n "$CHAIN_ID" || { echo "WEB_PARAMS=FAIL missing ARCHON_FEATURE_CHAIN_ID"; exit 1; }
case "$FEATURE_SCOPE" in
  fullstack)
    test -n "$HANDOFF" || { echo "WEB_PARAMS=FAIL no API handoff path in run message"; exit 1; }
    case "$HANDOFF" in /*) : ;; *) echo "WEB_PARAMS=FAIL API handoff path must be absolute, got: $HANDOFF"; exit 1 ;; esac
    test -f "$HANDOFF" || { echo "WEB_PARAMS=FAIL API handoff missing: $HANDOFF"; exit 1; }
    python3 "$LAYER/setup/archon-run.py" verify-feature-handoff --provider "$PROVIDER" --lane "$LANE" --artifacts "$AD" "$HANDOFF"
    ;;
  web) : ;;
  repositories) : ;;
  *) echo "WEB_PARAMS=FAIL unsupported feature scope $FEATURE_SCOPE"; exit 1 ;;
esac
APIPORT=""; WEBPORT=""
PORT_KEY="$(basename "${HANDOFF:-$CHAIN_ID}")"
[ -n "$APIBASE" ] && { APIPORT=$(bash "$LAYER/setup/port-alloc.sh" "$APIBASE" "$PORT_KEY") || { echo "WEB_PARAMS=FAIL cannot allocate api port from base $APIBASE"; exit 1; }; }
[ -n "$WEBBASE" ] && { WEBPORT=$(bash "$LAYER/setup/port-alloc.sh" "$WEBBASE" "$PORT_KEY") || { echo "WEB_PARAMS=FAIL cannot allocate web port from base $WEBBASE"; exit 1; }; }
python3 - "$ROOT" "$HANDOFF" "$AD" "$APIPORT" "$WEBPORT" <<'PY'
import hashlib, json, os, re, subprocess, sys
from pathlib import Path

def canonical_digest(path):
    return hashlib.sha256(json.dumps(json.loads(path.read_text(encoding="utf-8")), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def require_browser_policy(adir, expected):
    policy = adir / "browser-evidence.json"
    digest_file = adir / "browser-evidence.sha256"
    if not policy.is_file() or not digest_file.is_file():
        raise SystemExit("WEB_PARAMS=FAIL missing approved browser evidence policy")
    actual = canonical_digest(policy)
    try:
        approved = digest_file.read_text(encoding="utf-8").strip().split()[0].lower()
    except IndexError:
        approved = ""
    if not re.fullmatch(r"[0-9a-f]{64}", approved) or approved != actual:
        raise SystemExit("WEB_PARAMS=FAIL approved browser evidence policy digest mismatch")
    if expected != actual:
        raise SystemExit("WEB_PARAMS=FAIL handoff browser evidence policy hash mismatch")
    return actual

root, handoff, ad = map(Path, sys.argv[1:4])
api_port, web_port = sys.argv[4], sys.argv[5]
feature_scope = os.environ.get("ARCHON_FEATURE_SCOPE", "fullstack")
data = {}
apiwt = root / "api"
api_artifacts = None
web_allowlist = None
if feature_scope == "fullstack":
    data = json.loads(handoff.read_text(encoding="utf-8"))
    body = {k: v for k, v in data.items() if k not in {"handoff_sha256", "handoff_mac"}}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canon).hexdigest() != data.get("handoff_sha256"):
        raise SystemExit("WEB_PARAMS=FAIL API handoff SHA mismatch")
    if data.get("kind") != "archon-feature-api-handoff" or data.get("schema_version") != 2:
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
    if not isinstance(web_files, list) or not all(isinstance(x, str) and x.strip() for x in web_files):
        raise SystemExit("WEB_PARAMS=FAIL approved web-files-allowlist.json is malformed")
    for key, name in (("web_premises_sha256", "web-premises.json"), ("web_reader_audit_sha256", "web-reader-audit.json")):
        expected = data.get(key)
        artifact = api_artifacts / name
        if not isinstance(expected, str) or not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
            raise SystemExit(f"WEB_PARAMS=FAIL approved {name} hash mismatch")
    browser_expected = data.get("browser_evidence_sha256")
    if not isinstance(browser_expected, str):
        raise SystemExit("WEB_PARAMS=FAIL handoff missing browser evidence policy hash")
    require_browser_policy(api_artifacts, browser_expected)
    head = subprocess.run(["git", "-C", str(apiwt), "rev-parse", "HEAD"], capture_output=True, encoding="utf-8")
    if head.returncode != 0 or head.stdout.strip() != data.get("api_head_sha"):
        raise SystemExit("WEB_PARAMS=FAIL API handoff head changed")
elif feature_scope == "web":
    spec = handoff
    if not spec.is_file():
        raise SystemExit("WEB_PARAMS=FAIL standalone web spec missing")
    head = subprocess.run(["git", "-C", str(apiwt), "rev-parse", "HEAD"], capture_output=True, encoding="utf-8")
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip(), re.I):
        raise SystemExit("WEB_PARAMS=FAIL cannot pin standalone API baseline")
    data = {"api_head_sha": head.stdout.strip(), "api_branch": "controller-pinned-read-only-baseline"}
elif feature_scope == "repositories":
    phase = os.environ.get("ARCHON_FEATURE_PHASE")
    repo = os.environ.get("ARCHON_FEATURE_REPO")
    if phase != "implement" or repo != "web-app":
        raise SystemExit("WEB_PARAMS=FAIL repository-chain web lane requires PHASE=implement REPO=web-app")
    params_path = ad / "params.json"
    if not params_path.is_file():
        raise SystemExit("WEB_PARAMS=FAIL repository-chain params.json must be prebound by controller")
    params = json.loads(params_path.read_text(encoding="utf-8"))
    if params.get("feature_scope") != "repositories" or params.get("repo") != "web-app":
        raise SystemExit("WEB_PARAMS=FAIL repository-chain params.json has wrong scope or repo")
    repositories = params.get("repositories")
    if not isinstance(repositories, list) or "web-app" not in repositories:
        raise SystemExit("WEB_PARAMS=FAIL repository-chain params.json missing selected web-app")
    spec = Path(str(params.get("spec", "")))
    if not spec.is_absolute() or not spec.is_file():
        raise SystemExit("WEB_PARAMS=FAIL repository-chain spec missing")
    worktree = Path(str(params.get("worktree", "")))
    if not worktree.is_absolute() or not (worktree / ".git").exists():
        raise SystemExit("WEB_PARAMS=FAIL repository-chain web worktree missing")
    by_repo = params.get("worktrees_by_repo")
    if not isinstance(by_repo, dict) or by_repo.get("web-app") != str(worktree):
        raise SystemExit("WEB_PARAMS=FAIL repository-chain worktrees_by_repo does not match web worktree")
    joint = json.loads((ad / "joint-plan.json").read_text(encoding="utf-8")) if (ad / "joint-plan.json").is_file() else {}
    stage = joint.get("stages", {}).get("web-app", {}) if isinstance(joint.get("stages"), dict) else {}
    depends_on = stage.get("depends_on", [])
    if not isinstance(depends_on, list):
        raise SystemExit("WEB_PARAMS=FAIL repository-chain web stage dependencies malformed")
    uses_api = "api" in depends_on
    required = ["joint-plan.json", "plan.md", "files-allowlist.json", "verify.json"]
    for name in required:
        path = ad / name
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(f"WEB_PARAMS=FAIL repository-chain missing controller artifact {name}")
    files = json.loads((ad / "files-allowlist.json").read_text(encoding="utf-8"))
    verify = json.loads((ad / "verify.json").read_text(encoding="utf-8"))
    if not isinstance(files, list) or not all(isinstance(p, str) and p.strip() for p in files):
        raise SystemExit("WEB_PARAMS=FAIL repository-chain files-allowlist.json malformed")
    if not isinstance(verify, dict) or not isinstance(verify.get("test_patterns"), list) or not isinstance(verify.get("verification"), list):
        raise SystemExit("WEB_PARAMS=FAIL repository-chain verify.json malformed")
    api_candidate = None
    candidates_path = ad / "candidate-revisions.json"
    if candidates_path.is_file():
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        repos_obj = candidates.get("repositories")
        if not isinstance(repos_obj, dict):
            raise SystemExit("WEB_PARAMS=FAIL repository-chain candidate-revisions.json malformed")
        api_candidate = repos_obj.get("api")
        if uses_api and not isinstance(api_candidate, dict):
            raise SystemExit("WEB_PARAMS=FAIL repository-chain missing approved api candidate")
    if uses_api:
        if "api" not in repositories or "api" not in by_repo:
            raise SystemExit("WEB_PARAMS=FAIL repository-chain api dependency is outside selected scope")
        if not isinstance(api_candidate, dict):
            raise SystemExit("WEB_PARAMS=FAIL repository-chain missing candidate-revisions.json for api dependency")
        apiwt = Path(str(api_candidate.get("source_worktree", "")))
        if str(apiwt) != str(by_repo["api"]):
            raise SystemExit("WEB_PARAMS=FAIL repository-chain api candidate worktree does not match controller params")
        expected_api_head = api_candidate.get("commit")
        if not isinstance(expected_api_head, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_api_head, re.I):
            raise SystemExit("WEB_PARAMS=FAIL repository-chain api candidate commit malformed")
    else:
        fixture = params.get("api_fixture_worktree")
        fixture_head = params.get("api_fixture_head_sha")
        if not isinstance(fixture, str) or not isinstance(fixture_head, str):
            raise SystemExit("WEB_PARAMS=FAIL repository-chain web stage without api dependency requires approved api fixture")
        apiwt = Path(fixture)
        expected_api_head = fixture_head
    if not apiwt.is_absolute() or not (apiwt / ".git").exists():
        raise SystemExit("WEB_PARAMS=FAIL repository-chain API candidate worktree missing")
    head = subprocess.run(["git", "-C", str(apiwt), "rev-parse", "HEAD"], capture_output=True, encoding="utf-8")
    if head.returncode != 0 or head.stdout.strip() != expected_api_head:
        raise SystemExit("WEB_PARAMS=FAIL repository-chain API candidate head does not match approved revision")
    data = {
        "kind": "archon-repository-chain-local-candidates",
        "schema_version": 1,
        "logical_chain_id": os.environ.get("ARCHON_FEATURE_CHAIN_ID"),
        "api_head_sha": expected_api_head,
        "api_branch": subprocess.run(["git", "-C", str(apiwt), "branch", "--show-current"], capture_output=True, encoding="utf-8").stdout.strip(),
        "api_worktree": str(apiwt),
        "api_source": "candidate-revisions" if uses_api else "approved-fixture",
        "repositories": repositories,
    }
    params["api_worktree"] = str(apiwt)
    params["api_head_sha"] = expected_api_head
    params["api_branch"] = data["api_branch"] or "detached-local-candidate"
    params["logical_chain_id"] = data["logical_chain_id"]
    if api_port:
        params["api_port"] = int(api_port)
    if web_port:
        params["web_port"] = int(web_port)
    params_path.write_text(json.dumps(params, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (ad / "api-handoff.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not (ad / "web-files-allowlist.json").exists():
        (ad / "web-files-allowlist.json").write_bytes((ad / "files-allowlist.json").read_bytes())
    if not (ad / "premises.json").exists():
        (ad / "premises.json").write_text("[]\n", encoding="utf-8")
    if not (ad / "reader-audit.json").exists():
        (ad / "reader-audit.json").write_text('{"columns": []}\n', encoding="utf-8")
    print(f"WEB_PARAMS=OK scope=repositories spec={spec} slug={params.get('slug')} api_candidate={data['api_head_sha']} api_port={params.get('api_port', 'none')} web_port={params.get('web_port', 'none')}")
    raise SystemExit(0)
else:
    raise SystemExit(f"WEB_PARAMS=FAIL unsupported feature scope {feature_scope}")
slug = re.sub(r"[^a-z0-9]+", "-", spec.stem.lower()).strip("-")[:55]
if not slug:
    raise SystemExit("WEB_PARAMS=FAIL empty slug")
params = {
    "spec": str(spec),
    "slug": slug,
    "branch": f"archon/{slug}-web",
    "worktree": str(root / "web-app" / ".worktrees" / f"{slug}-web"),
    "handoff": str(handoff) if feature_scope == "fullstack" else None,
    "feature_scope": feature_scope,
    "api_worktree": str(apiwt),
    "api_branch": data.get("api_branch"),
    "api_head_sha": data.get("api_head_sha"),
    "api_pr_url": data.get("api_pr_url"),
    "api_run_id": data.get("api_run_id"),
    "logical_chain_id": data.get("logical_chain_id") or os.environ.get("ARCHON_FEATURE_CHAIN_ID"),
    "shared_plan_sha256": data.get("shared_plan_sha256"),
    "web_files_allowlist_sha256": data.get("web_files_allowlist_sha256"),
}
if api_port:
    params["api_port"] = int(api_port)
if web_port:
    params["web_port"] = int(web_port)
ad.mkdir(parents=True, exist_ok=True)
(ad / "params.json").write_text(json.dumps(params, indent=2) + "\n", encoding="utf-8")
if feature_scope == "fullstack":
    (ad / "api-handoff.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # The web lane inherits approved API-planning artifacts byte-for-byte. The
    # implementation node may use this allowlist, but cannot author or broaden it.
    assert web_allowlist is not None and api_artifacts is not None
    (ad / "files-allowlist.json").write_bytes(web_allowlist.read_bytes())
    (ad / "web-files-allowlist.json").write_bytes(web_allowlist.read_bytes())
    for src_name, dst_name in (("web-premises.json", "premises.json"), ("web-reader-audit.json", "reader-audit.json")):
        src = api_artifacts / src_name
        if not src.is_file():
            raise SystemExit(f"WEB_PARAMS=FAIL missing approved {src_name}")
        (ad / dst_name).write_bytes(src.read_bytes())
    for name in ("browser-evidence.json", "browser-evidence.sha256"):
        src = api_artifacts / name
        if not src.is_file():
            raise SystemExit(f"WEB_PARAMS=FAIL missing approved {name}")
        (ad / name).write_bytes(src.read_bytes())
else:
    (ad / "api-handoff.json").write_text(json.dumps({"mode": "standalone-web", "api_baseline": params.get("api_head_sha")}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (ad / "files-allowlist.json").write_text("[]\n", encoding="utf-8")
    (ad / "web-files-allowlist.json").write_text("[]\n", encoding="utf-8")
    (ad / "premises.json").write_text("[]\n", encoding="utf-8")
    (ad / "reader-audit.json").write_text('{"columns": []}\n', encoding="utf-8")
print(f"WEB_PARAMS=OK scope={feature_scope} spec={spec} slug={slug} api_run={data.get('api_run_id')} api_port={api_port or 'none'} web_port={web_port or 'none'}")
PY
