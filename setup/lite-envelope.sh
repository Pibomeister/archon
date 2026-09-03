#!/usr/bin/env bash
# Lite-lane routing envelope — the fail-closed gate that decides whether a
# ticket may stay on a lite lane (full-sdlc-api-lite / bugfix-lite).
#
# Usage: lite-envelope.sh <goodword-root> <artifacts-dir> <api|bugfix> <pre|plan|post>
#
# Reads setup/lite-envelope.json (the single source of truth for thresholds)
# plus the run's artifacts, prints one `ENVELOPE <check>=<value> OK` line per
# check evaluated at this stage (`ENVELOPE <check>=FAIL <detail>` for a failing
# one; every check is evaluated so all reasons are visible), and ends with
# `ROUTE=LITE` (exit 0) or `ROUTE=FULL reason=<check>[,<check>...]` (exit 1). The same lines are written to
# <artifacts-dir>/envelope-<stage>.txt on BOTH outcomes, because node stdout is
# not available after `archon workflow resume`.
#
# Fail-closed rules: a missing or malformed input is `ROUTE=FULL reason=malformed`,
# never a pass. An unavailable signal (impact.json status UNAVAILABLE) is
# `ROUTE=FULL reason=impact`. Nothing here calls Claude or MCP; the producers
# of triage.json / triage-post.json / impact.json are Claude nodes upstream.
set -uo pipefail

ROOT="${1:?usage: lite-envelope.sh <root> <artifacts-dir> <api|bugfix> <pre|plan|post>}"
AD="${2:?usage: lite-envelope.sh <root> <artifacts-dir> <api|bugfix> <pre|plan|post>}"
LANE="${3:?usage: lite-envelope.sh <root> <artifacts-dir> <api|bugfix> <pre|plan|post>}"
STAGE="${4:?usage: lite-envelope.sh <root> <artifacts-dir> <api|bugfix> <pre|plan|post>}"
ENVELOPE="${LITE_ENVELOPE_JSON:-$(cd "$(dirname "$0")" && pwd)/lite-envelope.json}"

OUT="$AD/envelope-$STAGE.txt"
python3 - "$ROOT" "$AD" "$LANE" "$STAGE" "$ENVELOPE" > "$OUT.tmp" 2>&1 <<'PY'
import json, os, re, sys

root, ad, lane, stage, envelope_path = sys.argv[1:6]
lines = []


def out(s):
    lines.append(s)


FAILS = []


def full(reason, detail=""):
    """A failed CHECK is recorded and evaluation continues, so the operator and
    the calibration table see every reason at once (a hot-path hit hidden
    behind a triage refusal is evidence lost). A MALFORMED input stops at
    once: nothing downstream can be trusted. Exactly one ROUTE line is
    printed either way, and the first reason leads it."""
    out(f"ENVELOPE {reason}=FAIL {detail}".rstrip())
    FAILS.append(reason)
    if reason == "malformed":
        finish()


def finish():
    if FAILS:
        out(f"ROUTE=FULL reason={','.join(dict.fromkeys(FAILS))}")
        print("\n".join(lines))
        sys.exit(1)
    out("ROUTE=LITE")
    print("\n".join(lines))
    sys.exit(0)


def load_json(name, required=True):
    p = os.path.join(ad, name)
    if not os.path.isfile(p):
        if required:
            full("malformed", f"{name} missing")
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:  # noqa: BLE001
        full("malformed", f"{name} unparsable: {e}")


if lane not in ("api", "bugfix"):
    full("malformed", f"unknown lane {lane}")
if stage not in ("pre", "plan", "post"):
    full("malformed", f"unknown stage {stage}")
if lane == "api" and stage == "pre":
    full("malformed", "api lane has no pre stage")

try:
    with open(envelope_path, encoding="utf-8") as fh:
        env = json.load(fh)
except Exception as e:  # noqa: BLE001
    full("malformed", f"lite-envelope.json unreadable: {e}")

for k in ("max_files", "max_test_files", "max_d1_callers", "max_chain_links"):
    v = env.get(k)
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        full("malformed", f"lite-envelope.json {k} is not a non-negative integer")
hot_paths = env.get("hot_paths")
allow = env.get("repro_command_allow")
if not isinstance(hot_paths, list) or not all(isinstance(h, str) and h for h in hot_paths):
    full("malformed", "lite-envelope.json hot_paths is not a list of non-empty strings")
if not isinstance(allow, list) or not all(isinstance(a, str) and a for a in allow):
    full("malformed", "lite-envelope.json repro_command_allow is not a list of non-empty strings")

params = load_json("params.json")
spec_path = params.get("spec") if isinstance(params, dict) else None
if not isinstance(spec_path, str) or not os.path.isfile(spec_path):
    full("malformed", "params.json has no readable spec path")
with open(spec_path, encoding="utf-8") as fh:
    spec_text = fh.read()
lite_ok = any(l.strip() == "Lane: lite-ok" for l in spec_text.splitlines())

TEST_RE = re.compile(r"(/__tests__/|\.(spec|test|int\.spec|e2e\.spec)\.tsx?$)")


def canonical(repo, entry):
    if not isinstance(entry, str) or not entry.strip():
        full("malformed", "allowlist entry is not a non-empty string")
        return f"{repo}/"
    e = entry.strip().replace("\\", "/")
    # Absolute paths are judged on the RAW entry: the lstrip below would erase
    # the evidence.
    if e.startswith("/"):
        full("malformed", f"allowlist entry must be repo-relative (absolute path): {entry}")
        return f"{repo}/"
    # Normalise first, then judge: "./api/x", ".//api/x", "././api/x" are the
    # same rooted entry as "api/x" and must not slip past on a prefix.
    e = re.sub(r"/{2,}", "/", e)
    while e.startswith("./"):
        e = e[2:]
        e = re.sub(r"/{2,}", "/", e)
    e = e.lstrip("/")
    parts = e.split("/")
    if ".." in parts:
        full("malformed", f"allowlist entry escapes the repo: {entry}")
        return f"{repo}/"
    # The mirror of the `..` check: an entry that is ALREADY rooted (starts
    # with a repo name) would canonicalise to "<repo>/<repo>/..." and match
    # no hot path at all — a silent bypass of every hot-path rule.
    if e in ("", ".") or parts[0] == "":
        full("malformed", f"allowlist entry is empty after normalisation: {entry}")
        return f"{repo}/"
    if parts[0].lower() in ("api", "web-app", "mobile-app", "goodword-kb"):
        full("malformed", f"allowlist entry must be repo-relative (no leading repo name): {entry}")
        return f"{repo}/"
    return f"{repo}/{e}"


def hot_hit(path):
    for h in hot_paths:
        if h.endswith("/"):
            if path.startswith(h):
                return h
        elif path == h:
            return h
    return None


def check_triage(name):
    t = load_json(name)
    size = t.get("size") if isinstance(t, dict) else None
    if size not in ("S", "M", "L"):
        full("malformed", f"{name} size not in S|M|L")
    if size == "L":
        return full("triage", f"{name} size=L")
    if size == "M" and not lite_ok:
        return full("triage", f"{name} size=M without 'Lane: lite-ok' in the spec")
    out(f"ENVELOPE triage={size}{' override=lite-ok' if size == 'M' else ''} OK")


def check_files(repo, entries):
    canon = [canonical(repo, e) for e in entries]
    code = [c for c in canon if not TEST_RE.search(c)]
    tests = [c for c in canon if TEST_RE.search(c)]
    if len(code) > env["max_files"]:
        full("files", f"{len(code)}/{env['max_files']} non-test files")
    else:
        out(f"ENVELOPE files={len(code)}/{env['max_files']} OK")
    if len(tests) > env["max_test_files"]:
        full("test_files", f"{len(tests)}/{env['max_test_files']} test files")
    else:
        out(f"ENVELOPE test_files={len(tests)}/{env['max_test_files']} OK")
    hits = [(c, h) for c in canon for h in [hot_hit(c)] if h]
    if hits:
        full("hot_paths", "; ".join(f"{c} matches {h}" for c, h in hits))
    else:
        out("ENVELOPE hot_paths=0 OK")


def check_impact():
    imp = load_json("impact.json")
    status = imp.get("status") if isinstance(imp, dict) else None
    if status not in ("GATHERED", "UNAVAILABLE", "SKIPPED"):
        full("malformed", "impact.json status not in GATHERED|UNAVAILABLE|SKIPPED")
    if status == "UNAVAILABLE":
        full("impact", "impact.json status=UNAVAILABLE (gitnexus unavailable in the node session)")
    else:
        out(f"ENVELOPE impact={status} OK")
    syms = imp.get("symbols")
    if not isinstance(syms, list):
        full("malformed", "impact.json symbols is not a list")
    if status == "GATHERED" and not syms:
        full("impact", "impact.json status=GATHERED but symbols is empty (new symbols must use SKIPPED)")
    if status in ("UNAVAILABLE", "SKIPPED") and syms:
        full("malformed", f"impact.json status={status} must carry an empty symbols list")
    d1 = 0
    for i, s in enumerate(syms):
        if not isinstance(s, dict):
            full("malformed", f"impact.json symbols[{i}] is not an object")
        name = s.get("name")
        file = s.get("file")
        risk = s.get("risk")
        if not isinstance(name, str) or not name.strip() or not isinstance(file, str) or not file.strip():
            full("malformed", f"impact.json symbols[{i}] needs non-empty name/file")
        if risk not in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
            full("malformed", f"impact.json symbols[{i}] risk out of enum")
        callers = s.get("d1_callers") if isinstance(s, dict) else None
        if not isinstance(callers, list) or not all(isinstance(c, str) and c.strip() for c in callers):
            full("malformed", "impact.json symbol without d1_callers list")
        if status == "GATHERED":
            query_status = s.get("query_status")
            query_repo = s.get("query_repo")
            query_target = s.get("query_target")
            if query_status != "GATHERED":
                full("impact", f"symbol {name!r} query_status={query_status!r}; every graph query must succeed")
            if query_repo != "api":
                full("impact", f"symbol {name!r} query_repo={query_repo!r}; expected the pinned api index")
            if not isinstance(query_target, str) or not query_target.strip():
                full("impact", f"symbol {name!r} has no query_target provenance")
        d1 += len(callers)
    if d1 > env["max_d1_callers"]:
        full("d1_callers", f"{d1}/{env['max_d1_callers']}")
    else:
        out(f"ENVELOPE d1_callers={d1}/{env['max_d1_callers']} OK")


# ---------------------------------------------------------------- bugfix/pre
if lane == "bugfix" and stage == "pre":
    ep = load_json("evidence-plan.json")
    if not isinstance(ep, dict):
        full("malformed", "evidence-plan.json is not an object")
    cmd = ep.get("repro_command")
    obs = ep.get("repro_observed")
    if not isinstance(cmd, str) or not cmd.strip():
        full("repro", "evidence-plan.json repro_command missing — the report needs a '## Repro' block")
    elif "\n" in cmd or re.search(r"[;&|$`<>]", cmd):
        full("repro", "repro_command contains a shell metacharacter or newline")
    elif not any(cmd.startswith(a) for a in allow):
        full("repro", f"repro_command not in repro_command_allow: {cmd}")
    elif not isinstance(obs, str) or not obs.strip():
        full("repro", "repro_observed missing — paste the observed failure output")
    else:
        out("ENVELOPE repro=given OK")
    hint = ep.get("repo_hint")
    if hint not in ("api", "web-app", "unknown"):
        full("malformed", "evidence-plan.json repo_hint not in api|web-app|unknown")
    if hint == "unknown":
        full("repo_hint", "repo_hint=unknown")
    else:
        out(f"ENVELOPE repo_hint={hint} OK")
    check_triage("triage.json")
    finish()

# ------------------------------------------------------------ plan / post
triage_name = "triage-post.json" if stage == "post" else "triage.json"

if lane == "api":
    if re.search(r"^## Premises to verify\s*$", spec_text, re.M):
        full("premises", "spec carries '## Premises to verify' — premises are the full lane's contract")
    else:
        out("ENVELOPE premises=none OK")
    check_triage(triage_name)
    fa = load_json("files-allowlist.json")
    if not isinstance(fa, list) or not fa:
        full("malformed", "files-allowlist.json is not a non-empty list")
    check_files("api", fa)
    ra = load_json("reader-audit.json")
    cols = ra.get("columns") if isinstance(ra, dict) else None
    if not isinstance(cols, list):
        full("malformed", "reader-audit.json columns is not a list")
    if cols:
        full("reader_audit", f"{len(cols)} column(s) declared — the reader-audit node is not on the lite lane")
    else:
        out("ENVELOPE reader_audit=0 OK")
    check_impact()
else:
    check_triage(triage_name)
    rj = load_json("repo.json")
    repo = rj.get("repo") if isinstance(rj, dict) else None
    if repo not in ("api", "web-app", "both"):
        full("malformed", "repo.json repo not in api|web-app|both")
    if repo == "both":
        full("repo", "repo.json repo=both (cross-repo is a full-lane stop)")
        repo = "api"
    else:
        out(f"ENVELOPE repo={repo} OK")
    fp = load_json("fix-plan.json")
    files = fp.get("files") if isinstance(fp, dict) else None
    if not isinstance(files, list) or not files:
        full("malformed", "fix-plan.json files is not a non-empty list")
    fa = load_json("files-allowlist.json", required=False)
    entries = list(files)
    if fa is not None:
        if not isinstance(fa, list):
            full("malformed", "files-allowlist.json is not a list")
        entries += [e for e in fa if e not in entries]
    check_files(repo, entries)
    cc = load_json("causal-chain.json")
    links = cc.get("links") if isinstance(cc, dict) else None
    if not isinstance(links, list) or not links:
        full("malformed", "causal-chain.json links is not a non-empty list")
    if len(links) > env["max_chain_links"]:
        full("chain_links", f"{len(links)}/{env['max_chain_links']}")
    else:
        out(f"ENVELOPE chain_links={len(links)}/{env['max_chain_links']} OK")
    check_impact()

finish()
PY
RC=$?
# Persist on both outcomes, then echo. A python crash (rc other than 0/1)
# still leaves its traceback in the file and is reported as malformed.
if [ "$RC" -ne 0 ] && [ "$RC" -ne 1 ]; then
  echo "ROUTE=FULL reason=malformed lite-envelope.sh internal error rc=$RC" >> "$OUT.tmp"
  RC=1
fi
mv "$OUT.tmp" "$OUT"
cat "$OUT"
exit "$RC"
