"""Determinism harness for Archon workflow bash nodes.

`run_node` extracts a node body with `nodes.extract.runnable_body`, builds a
FRESH fixture (artifacts dir, optional throwaway git worktree, optional PATH
shims) for every repetition, runs the body N times, and asserts that all N
runs produced byte-identical observations.

An observation is deliberately narrow — the three things the pipeline actually
contracts on:

  1. the exit code,
  2. the set of TYPED lines the node printed (RUNBOOK.md section 3/3a/3b
     vocabulary; these are what operators and `grep` route on),
  3. every file under ARTIFACTS_DIR, by relative path, with its contents
     normalized.

Normalization removes the three things that legitimately differ between two
runs of the same node: git object shas, timestamps, and the temp directory the
fixture happens to live in. Anything else that differs is a finding.

The second contract this enforces is that every exit is TYPED: a non-zero exit
must have printed a FAIL-class line and a zero exit a PASS-class line. An exit
with neither is an `untyped_exit` — RUNBOOK.md cannot route it, so the operator
gets a bare stack of shell output and no discriminator.

N comes from the NODE_STRESS env var (default 3, so the committed suite stays
fast). `NODE_STRESS=100 python3 -m unittest discover -s setup/tests -p
'test_node_stress.py'` is the SLA sweep.
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from nodes.extract import runnable_body

DEFAULT_N = 3

# --- typed-line vocabulary ------------------------------------------------
# Assignment form: KEY=VALUE. The key may carry a lowercase tail
# (GATE_3_verdict_in_enum=PASS), so the key pattern is permissive and the
# CLASS comes from the value.
PASS_VALUES = {"PASS", "OK", "CLEAN", "SKIP"}
FAIL_VALUES = {"FAIL", "DIRTY"}

# Bare-token form: the loop discriminators. These carry their meaning in the
# token itself (RUNBOOK.md section 3 "the four discriminators", 3a, 3b), so
# they are enumerated rather than pattern-matched — a typo in a workflow body
# should read as an untyped exit, not silently classify.
PASS_TOKENS = {
    "PLAN_ROUND",                    # plan-round-pre success (PLAN_ROUND=N cap=C)
    "RCA_PLAN_ROUND",                # rca-round-pre success
    "PLAN_CONVERGED", "RCA_PLAN_CONVERGED",
    "PLAN_ROUND_PROGRESSED", "RCA_PLAN_ROUND_PROGRESSED",
    "CONVERGED", "ROUND_PROGRESSED",
    "CRITIQUE",                      # parse-critique.py success line
    "GREEN_CHECK",                   # green-check ALWAYS exits 0 by design:
                                     # fix-converge owns the verdict, this node
                                     # only measures. green=false is still a
                                     # typed, successful exit for this node.
}
FAIL_TOKENS = {
    "PLAN_ROUND_CAP", "RCA_PLAN_ROUND_CAP", "ROUND_CAP_REACHED", "DESLOP_ROUND_CAP",
    "PLAN_REJECTED", "RCA_PLAN_REJECTED",
    "PLAN_NO_PROGRESS", "RCA_PLAN_NO_PROGRESS", "NO_PROGRESS",
    "PLAN_SCOPE_DISPUTE", "RCA_PLAN_SCOPE_DISPUTE",
    "FIXER_BLOCKED", "SCOPE_BREACH",
}

_TYPED_RE = re.compile(r"^(?P<key>[A-Z][A-Za-z0-9_]{2,})(?:=(?P<val>\S*))?(?:\s|$)")

# --- normalizers ----------------------------------------------------------
_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
# Anything left that looks like a per-run scratch path. The fixture root is
# substituted by exact string first; this catches paths the body derived.
_TMPPATH_RE = re.compile(r"(?:/private)?/(?:var/folders/[^\s\"']*|tmp/[^\s\"']*)")


def _parse_typed(line):
    """(key, value) if `line` is a typed line, else None.

    The single place the typed-line grammar is applied, so `classify` reads the
    same parse `_typed_lines` selected on rather than re-matching and assuming
    the match succeeded.
    """
    m = _TYPED_RE.match(line)
    if m is None:
        return None
    key = m.group("key")
    # A leading token is "typed" if it is SCREAMING_SNAKE, or an
    # underscore-joined identifier that starts screaming (GATE_3_verdict...).
    if not (key.isupper() or "_" in key):
        return None
    return key, m.group("val")


def _typed_lines(text):
    """Every typed line in `text`, in the order printed."""
    return [line.rstrip() for line in text.splitlines()
            if _parse_typed(line) is not None]


def classify(lines):
    """(has_pass, has_fail) over a list of typed lines."""
    has_pass = has_fail = False
    for line in lines:
        parsed = _parse_typed(line)
        if parsed is None:
            continue
        key, val = parsed
        if val is not None:
            if val in PASS_VALUES:
                has_pass = True
            elif val in FAIL_VALUES:
                has_fail = True
        if key in PASS_TOKENS:
            has_pass = True
        elif key in FAIL_TOKENS:
            has_fail = True
    return has_pass, has_fail


def normalize(text, tmp):
    text = text.replace(str(tmp), "<TMP>")
    real = os.path.realpath(str(tmp))
    if real != str(tmp):
        text = text.replace(real, "<TMP>")
    text = _TMPPATH_RE.sub("<TMP>", text)
    text = _SHA_RE.sub("<SHA>", text)
    text = _TS_RE.sub("<TS>", text)
    return text


def _tar_digest(path):
    """Content identity of a tar, without its headers' wall-clock mtime.

    `git archive <tree>` stamps the CURRENT time into every member header when
    it archives a bare tree (there is no commit to take a date from), so two
    runs over an identical tree produce different bytes. The bytes are not a
    contract — the checkpoint TREE SHA is, and it is compared separately — so
    the harness compares what the tar actually restores instead: every member's
    name, type, mode, size and content hash. A changed file still shows up.
    """
    import hashlib
    import tarfile

    rows = []
    with tarfile.open(path) as tf:
        for m in sorted(tf.getmembers(), key=lambda m: m.name):
            data = b""
            if m.isfile():
                f = tf.extractfile(m)
                data = f.read() if f else b""
            rows.append(
                f"{m.name}|{m.type.decode()}|{m.mode:o}|{m.size}|"
                f"{hashlib.sha256(data).hexdigest()}"
            )
    return "<TAR " + " ".join(rows) + ">"


def _snapshot(art, tmp):
    """Sorted [(relpath, normalized-content)] for every file under ARTIFACTS_DIR."""
    rows = []
    for path in sorted(Path(art).rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(art).as_posix()
        if rel.endswith(".tar"):
            rows.append((rel, _tar_digest(path)))
            continue
        raw = path.read_bytes()
        try:
            content = normalize(raw.decode("utf-8"), tmp)
        except UnicodeDecodeError:
            # git index files and `git archive` tars are binary AND carry
            # per-run bytes (archive mtime, index stat data). Size is the
            # deterministic part; see AUDIT.md.
            content = f"<BINARY bytes={len(raw)}>"
        rows.append((rel, content))
    return tuple(rows)


_BODY_CACHE = {}
_BODY_LOCK = threading.Lock()


def _body_cached(workflow, node, outputs, root, subs):
    key = (workflow, node, root,
           tuple(sorted((outputs or {}).items())), tuple(map(tuple, subs or ())))
    with _BODY_LOCK:
        if key not in _BODY_CACHE:
            body = runnable_body(workflow, node, outputs=outputs, root=root)
            for old, new in (subs or []):
                if old not in body:
                    raise AssertionError(
                        f"{workflow}:{node} sub target not present in body: {old!r}"
                    )
                body = body.replace(old, new)
            _BODY_CACHE[key] = body
    return _BODY_CACHE[key]


def run_node(workflow, node, fixture_builder, n=None, outputs=None, env=None,
             subs=None, root=None):
    """Run one bash node N times against a freshly built fixture each time.

    fixture_builder(tmp) is called with an empty temp dir; by convention it
    populates `tmp/artifacts` (which the runner creates and exports as
    ARTIFACTS_DIR) and may create `tmp/bin` (auto-prepended to PATH) and a
    throwaway git worktree. It returns a dict of extra env vars, or None.

    `subs` is an ordered list of (old, new) applied to the body AFTER
    runnable_body's own root rewrite — for the handful of nodes that hardcode
    an absolute worktree path the engine never parameterized.

    Returns a summary dict; raises AssertionError on a determinism or typing
    violation.
    """
    n = n or int(os.environ.get("NODE_STRESS") or DEFAULT_N)
    # The body does not depend on the fixture, and parsing a 100KB workflow
    # YAML costs ~40ms; extract it once per (node, outputs, subs) instead of
    # once per repetition.
    body = _body_cached(workflow, node, outputs, root, subs)

    def one_run(_i):
        tmp = Path(tempfile.mkdtemp(prefix="nodestress-"))
        try:
            art = tmp / "artifacts"
            art.mkdir(exist_ok=True)
            extra = fixture_builder(tmp) or {}
            run_env = dict(os.environ)
            run_env.update(env or {})
            run_env.update(extra)
            run_env["ARTIFACTS_DIR"] = str(art)
            shim = tmp / "bin"
            if shim.is_dir():
                run_env["PATH"] = f"{shim}:{run_env['PATH']}"
            p = subprocess.run(["bash", "-c", body], capture_output=True,
                               encoding="utf-8", errors="replace", env=run_env,
                               cwd=str(tmp))
            combined = normalize((p.stdout or "") + (p.stderr or ""), tmp)
            lines = _typed_lines(combined)
            has_pass, has_fail = classify(lines)
            untyped = (p.returncode == 0 and not has_pass) or (
                p.returncode != 0 and not has_fail
            )
            obs = (p.returncode, tuple(sorted(set(lines))), _snapshot(art, tmp))
            return obs, (p.returncode, combined, lines), untyped
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # The repetitions run CONCURRENTLY on purpose: N copies of the same node
    # racing on the same host is exactly the shape that exposes shared-state
    # nondeterminism (a /tmp scan, a fixed port, a global lock). Each run still
    # gets its own temp root, so a difference is the node's, not the harness's.
    if n > 1:
        # Workers ~= cores: every run forks bash plus several python3/git
        # children, so oversubscribing threads only thrashes.
        with ThreadPoolExecutor(max_workers=min(n, os.cpu_count() or 4)) as pool:
            results = list(pool.map(one_run, range(n)))
    else:
        results = [one_run(0)]
    observations = [r[0] for r in results]
    firsts = results[0][1]
    untyped = sum(1 for r in results if r[2])

    identical = sum(1 for o in observations if o == observations[0])
    print(f"NODE_STRESS {workflow}:{node} N={n} identical={identical} untyped_exits={untyped}")
    if identical != n:
        idx = next(i for i, o in enumerate(observations) if o != observations[0])
        raise AssertionError(
            f"{workflow}:{node} nondeterministic across {n} runs "
            f"(run 0 vs run {idx}):\n{_diff(observations[0], observations[idx])}"
        )
    if untyped:
        rc, out, lines = firsts
        raise AssertionError(
            f"{workflow}:{node} untyped_exit rc={rc} in {untyped}/{n} runs; "
            f"typed lines seen={lines}\n--- output ---\n{out}"
        )
    return {
        "workflow": workflow, "node": node, "n": n, "identical": identical,
        "untyped_exits": untyped, "rc": firsts[0], "typed": firsts[2],
        "output": firsts[1],
    }


def _diff(a, b):
    parts = []
    if a[0] != b[0]:
        parts.append(f"rc: {a[0]} != {b[0]}")
    if a[1] != b[1]:
        parts.append(f"typed lines only in run 0: {sorted(set(a[1]) - set(b[1]))}")
        parts.append(f"typed lines only in run N: {sorted(set(b[1]) - set(a[1]))}")
    da, db = dict(a[2]), dict(b[2])
    for k in sorted(set(da) | set(db)):
        if da.get(k) != db.get(k):
            parts.append(f"file {k}:\n  run0={da.get(k)!r}\n  runN={db.get(k)!r}")
    return "\n".join(parts)
