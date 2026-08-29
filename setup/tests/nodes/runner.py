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

Normalization handles the three things that legitimately move between two runs
of the same node — git object shas, timestamps, and the temp dir the fixture
happens to live in — WITHOUT throwing them away. Each distinct value gets an
ordinal in first-appearance order (`<SHA:1>`, `<TS:1>`), so the comparison sees
the structure; and `_unstable_slots` then requires the raw value behind each
ordinal to be the same in every run unless the caller declares it
`volatile=(...)`. Collapsing every sha to a single `<SHA>` sentinel, as this
did originally, let a node that emits a FRESH random sha every run compare
identical — see test_runner_selfcheck.py, which keeps that failure as a
permanent negative control.

The second contract this enforces is that every exit is TYPED: a non-zero exit
must have printed a FAIL-class line and a zero exit a PASS-class line. An exit
with neither is an `untyped_exit` — RUNBOOK.md cannot route it, so the operator
gets a bare stack of shell output and no discriminator.

N comes from the NODE_STRESS env var (default 3, so the committed suite stays
fast). `NODE_STRESS=100 python3 -m unittest discover -s setup/tests -p
'test_node_stress.py'` is the SLA sweep.
"""
import hashlib
import os
import re
import shutil
import struct
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


def _isolation_env(tmp):
    """Cut the node off from this machine's ambient state.

    The node inherits `os.environ`, so without this it reads the developer's
    `$HOME/.gitconfig`, shares the real `$TMPDIR`, and picks up whatever else
    the invoking shell exported — none of which is an input the fixture
    controls, and all of which differs between a laptop and CI. A node that
    behaves identically here and differently on another machine is exactly what
    this harness exists to catch, so the ambient inputs are pinned rather than
    inherited. Callers can still override any of these.
    """
    home, tmpdir = tmp / "home", tmp / "tmpdir"
    home.mkdir(exist_ok=True)
    tmpdir.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
    }


def _observe_typed(lines):
    """The typed lines as the OBSERVATION sees them: original order, duplicates
    kept.

    Collapsing to `sorted(set(...))`, as this did originally, threw away two
    real signals — a node that prints its gates in a different order, and a
    node that prints the same line a different number of times (a loop that
    iterated twice instead of once). Both read as identical.
    """
    return tuple(lines)


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


class Normalizer:
    """Per-EXECUTION value map.

    An earlier version mapped every sha to `<SHA>`, every timestamp to `<TS>`
    and every scratch path to `<TMP>`. That collapsed too much: a node emitting
    a FRESH random sha on every run produced `<SHA>` every run and compared
    identical, so "identical=100" could not tell "reproducible" from "changes
    every time". Two things fix it, and both are needed:

    1. Ordinals in first-appearance order (`<SHA:1>`, `<SHA:2>`) preserve
       STRUCTURE — "the sha in the log is the same one written to the file" is
       now a fact the comparison can see, and a run that emits a different
       NUMBER or ARRANGEMENT of values no longer matches.
    2. The raw value behind each slot is kept, and `_unstable_slots` requires
       it to be the same in every run unless the caller declares it volatile.
       Ordinals alone are still blind to a single random value per run: one
       random sha is `<SHA:1>` in both runs.

    Feed order fixes the ordinals, so callers must normalize in a deterministic
    order (stdout+stderr first, then files by sorted relative path).

    The fixture root is substituted by exact string FIRST and becomes `<ROOT>`,
    never a slot: it is the harness's own per-run temp dir, not the node's
    output.
    """

    def __init__(self, tmp):
        self.tmp = str(tmp)
        self.real = os.path.realpath(self.tmp)
        self.slots = {}    # kind -> {ordinal: raw value}
        self._index = {}   # kind -> {raw value: ordinal}

    def _ordinal(self, kind, value):
        index = self._index.setdefault(kind, {})
        if value not in index:
            index[value] = len(index) + 1
            self.slots.setdefault(kind, {})[index[value]] = value
        return f"<{kind}:{index[value]}>"

    def __call__(self, text):
        text = text.replace(self.tmp, "<ROOT>")
        if self.real != self.tmp:
            text = text.replace(self.real, "<ROOT>")
        text = _TMPPATH_RE.sub(lambda m: self._ordinal("TMP", m.group(0)), text)
        text = _SHA_RE.sub(lambda m: self._ordinal("SHA", m.group(0)), text)
        text = _TS_RE.sub(lambda m: self._ordinal("TS", m.group(0)), text)
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
    import tarfile

    rows = []
    with tarfile.open(path) as tf:
        for m in sorted(tf.getmembers(), key=lambda m: m.name):
            data = b""
            if m.isfile():
                f = tf.extractfile(m)
                data = f.read() if f else b""
            # linkname carries the whole content of a symlink/hardlink member:
            # without it, `a -> secrets` and `a -> README` are the same tar.
            rows.append(
                f"{m.name}|{m.type.decode()}|{m.mode:o}|{m.size}|"
                f"{m.linkname}|{hashlib.sha256(data).hexdigest()}"
            )
    return "<TAR " + " ".join(rows) + ">"


def _git_index_digest(raw):
    """What a git index MEANS, without its per-file stat data.

    A git index entry carries ctime/mtime/dev/ino alongside the mode, blob sha
    and path. The stat fields differ on every run (new inode, new ctime) even
    for byte-identical content, so hashing the file compares noise. This reads
    the DIRC format and keeps the three fields that are the index's actual
    content — mode, blob sha, path — the same way `_tar_digest` keeps what a
    tar restores. Returns None if the bytes are not a parseable index, so an
    unexpected binary falls back to a content hash rather than being excused.
    """
    if len(raw) < 12 or raw[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", raw[4:12])
    if version not in (2, 3, 4) or count > 100000:
        return None
    rows, off = [], 12
    for _ in range(count):
        if off + 62 > len(raw):
            return None
        mode, = struct.unpack(">I", raw[off + 24:off + 28])
        blob = raw[off + 40:off + 60].hex()
        flags, = struct.unpack(">H", raw[off + 60:off + 62])
        namelen = flags & 0x0FFF
        if namelen == 0x0FFF:      # long path: length is not in the flags
            return None
        start = off + 62
        name = raw[start:start + namelen].decode("utf-8", "replace")
        rows.append(f"{mode:o}|{blob}|{name}")
        entry = 62 + namelen
        off += entry + (8 - entry % 8 or 8)   # pad to a multiple of 8
    return "<GITINDEX " + " ".join(rows) + ">"


def _binary_digest(raw):
    """Hash, never length: two different 16-byte files are not the same
    artifact, and a size-only compare said they were."""
    return f"<BINARY sha256={hashlib.sha256(raw).hexdigest()}>"


def _snapshot(art, norm):
    """Sorted [(relpath, normalized-content)] for every entry under ARTIFACTS_DIR.

    Iterated in sorted order because `norm` assigns slot ordinals in
    first-appearance order.
    """
    rows = []
    for path in sorted(Path(art).rglob("*")):
        rel = path.relative_to(art).as_posix()
        if path.is_symlink():
            # A symlink's content IS its target. Skipping them, as this did
            # originally, made `link -> a` and `link -> b` the same artifact.
            rows.append((rel, norm(f"<SYMLINK -> {os.readlink(path)}>")))
            continue
        if not path.is_file():
            continue
        if rel.endswith(".tar"):
            rows.append((rel, norm(_tar_digest(path))))
            continue
        raw = path.read_bytes()
        try:
            content = norm(raw.decode("utf-8"))
        except UnicodeDecodeError:
            content = _git_index_digest(raw)
            content = norm(content) if content is not None else _binary_digest(raw)
        rows.append((rel, content))
    return tuple(rows)


def _git(repo, *args, env=None):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, **(env or {})})
    return p.stdout if p.returncode == 0 else f"<git {args[0]} rc={p.returncode}>"


def _worktree_snapshot(tmp, norm):
    """The state of every throwaway git repo the fixture built.

    ARTIFACTS_DIR is only half of what a node produces: `deslop-commit` makes a
    commit, `deslop-recheck` stages a checkpoint, `converge`/`gate-tests` read
    and can leave the tree dirty, `green-check` runs the repro in it. A node
    that mutated the WORKTREE differently between runs was invisible to a
    snapshot that only walked the artifacts dir.

    Four facts, all content-addressed and all normalized through the same
    ordinal map:

      HEAD      - the commit the node left the branch on.
      WORKTREE  - a tree sha over the ENTIRE working tree, computed through a
                  throwaway index (`GIT_INDEX_FILE=… git add -A; git
                  write-tree`) — the same technique `deslop-recheck` uses for
                  its own checkpoint. This is the one that sees CONTENT:
                  HEAD/index/porcelain alone report that `src/foo.ts` is
                  modified, never what it was modified TO, so a node writing
                  random bytes into a tracked file was still invisible.
      INDEX     - `ls-files -s` (mode/sha/stage/path; mode 120000 entries carry
                  symlink targets), so STAGING moves are distinguishable from
                  working-tree ones.
      STATUS    - `status --porcelain`, which names the paths in a form a
                  failure message can be read against.

    Every top-level dir holding a `.git` is picked up, so a fixture cannot
    silently opt out by naming its worktree something other than `wt`.
    """
    rows = []
    for path in sorted(p for p in Path(tmp).iterdir() if p.is_dir()):
        if not (path / ".git").exists():
            continue
        # A scratch index OUTSIDE the worktree and outside ARTIFACTS_DIR, so
        # observing the tree cannot itself show up as an artifact or as
        # untracked drift. `add -A` only writes blobs; no ref or index of the
        # repo's own is touched.
        idx = Path(tmp) / f".obs-index-{path.name}"
        idx.unlink(missing_ok=True)
        genv = {"GIT_INDEX_FILE": str(idx)}
        _git(path, "add", "-A", env=genv)
        tree = _git(path, "write-tree", env=genv).strip()
        rows.append((
            f"worktree:{path.name}",
            norm(f"HEAD={_git(path, 'rev-parse', 'HEAD').strip()}\n"
                 f"WORKTREE={tree}\n"
                 + "INDEX\n" + _git(path, "ls-files", "-s")
                 + "STATUS\n" + _git(path, "status", "--porcelain")),
        ))
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
             subs=None, root=None, volatile=()):
    """Run one bash node N times against a freshly built fixture each time.

    fixture_builder(tmp) is called with an empty temp dir; by convention it
    populates `tmp/artifacts` (which the runner creates and exports as
    ARTIFACTS_DIR) and may create `tmp/bin` (auto-prepended to PATH) and a
    throwaway git worktree. It returns a dict of extra env vars, or None.

    `subs` is an ordered list of (old, new) applied to the body AFTER
    runnable_body's own root rewrite — for the handful of nodes that hardcode
    an absolute worktree path the engine never parameterized.

    `volatile` declares normalized slots (e.g. "SHA:1") whose RAW value is
    allowed to differ between runs — see `stress`.

    Returns a summary dict; raises AssertionError on a determinism or typing
    violation.
    """
    # The body does not depend on the fixture, and parsing a 100KB workflow
    # YAML costs ~40ms; extract it once per (node, outputs, subs) instead of
    # once per repetition.
    body = _body_cached(workflow, node, outputs, root, subs)
    summary = stress(f"{workflow}:{node}", body, fixture_builder, n=n, env=env,
                     volatile=volatile)
    summary["workflow"], summary["node"] = workflow, node
    return summary


def stress(label, body, fixture_builder, n=None, env=None, volatile=()):
    """Run a literal bash body N times against a freshly built fixture each
    time and assert the N observations are identical.

    This is the core `run_node` wraps; it takes a body directly so the harness
    can be tested against synthetic nodes (see test_runner_selfcheck.py).
    """
    n = n or int(os.environ.get("NODE_STRESS") or DEFAULT_N)
    volatile = set(volatile)

    def one_run(_i):
        tmp = Path(tempfile.mkdtemp(prefix="nodestress-"))
        try:
            art = tmp / "artifacts"
            art.mkdir(exist_ok=True)
            extra = fixture_builder(tmp) or {}
            run_env = dict(os.environ)
            run_env.update(_isolation_env(tmp))
            run_env.update(env or {})
            run_env.update(extra)
            run_env["ARTIFACTS_DIR"] = str(art)
            shim = tmp / "bin"
            if shim.is_dir():
                run_env["PATH"] = f"{shim}:{run_env['PATH']}"
            p = subprocess.run(["bash", "-c", body], capture_output=True,
                               encoding="utf-8", errors="replace", env=run_env,
                               cwd=str(tmp))
            # ONE normalizer per execution, fed in a fixed order: the slot
            # ordinals encode where each value appeared relative to the others.
            norm = Normalizer(tmp)
            combined = norm((p.stdout or "") + (p.stderr or ""))
            lines = _typed_lines(combined)
            has_pass, has_fail = classify(lines)
            untyped = (p.returncode == 0 and not has_pass) or (
                p.returncode != 0 and not has_fail
            )
            obs = (p.returncode, _observe_typed(lines),
                   _snapshot(art, norm) + _worktree_snapshot(tmp, norm))
            return obs, (p.returncode, combined, lines), untyped, norm.slots
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
    slotmaps = [r[3] for r in results]

    identical = sum(1 for o in observations if o == observations[0])
    unstable = _unstable_slots(slotmaps, volatile)
    print(f"NODE_STRESS {label} N={n} identical={identical} "
          f"untyped_exits={untyped} unstable_slots={len(unstable)}")
    if identical != n:
        idx = next(i for i, o in enumerate(observations) if o != observations[0])
        raise AssertionError(
            f"{label} nondeterministic across {n} runs "
            f"(run 0 vs run {idx}):\n{_diff(observations[0], observations[idx])}"
        )
    if unstable:
        raise AssertionError(
            f"{label} produced values that differ between runs. The STRUCTURE "
            f"matched, so these slipped past the ordinal comparison; each one "
            f"is either real nondeterminism or a value that must be declared "
            f"volatile=(...) with a reason:\n" + "\n".join(unstable)
        )
    if untyped:
        rc, out, lines = firsts
        raise AssertionError(
            f"{label} untyped_exit rc={rc} in {untyped}/{n} runs; "
            f"typed lines seen={lines}\n--- output ---\n{out}"
        )
    return {
        "label": label, "n": n, "identical": identical,
        "untyped_exits": untyped, "rc": firsts[0], "typed": firsts[2],
        "output": firsts[1], "slots": slotmaps[0],
    }


def _unstable_slots(slotmaps, volatile):
    """Slots whose RAW value was not the same in every run.

    The ordinal normalization deliberately compares STRUCTURE — "one sha here,
    the same sha there" — which is what lets a git commit sha that legitimately
    moves still compare equal. On its own that is too weak: a body emitting a
    FRESH random sha every run also produces "one sha here" every run and would
    read as identical. So the raw values behind each slot are compared too, and
    anything that moves must be declared `volatile` with a reason.
    """
    bad = []
    for kind in sorted({k for m in slotmaps for k in m}):
        ordinals = sorted({o for m in slotmaps for o in m.get(kind, {})})
        for o in ordinals:
            seen = {m.get(kind, {}).get(o) for m in slotmaps}
            slot = f"{kind}:{o}"
            if len(seen) > 1 and slot not in volatile:
                vals = sorted(str(v) for v in seen)
                bad.append(f"  {slot} took {len(seen)} values across runs: "
                           f"{vals[:4]}{' …' if len(vals) > 4 else ''}")
    return bad


def _diff(a, b):
    parts = []
    if a[0] != b[0]:
        parts.append(f"rc: {a[0]} != {b[0]}")
    if a[1] != b[1]:
        parts.append(f"typed lines run 0: {list(a[1])}")
        parts.append(f"typed lines run N: {list(b[1])}")
    da, db = dict(a[2]), dict(b[2])
    for k in sorted(set(da) | set(db)):
        if da.get(k) != db.get(k):
            parts.append(f"file {k}:\n  run0={da.get(k)!r}\n  runN={db.get(k)!r}")
    return "\n".join(parts)
