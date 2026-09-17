#!/usr/bin/env python3
"""Durable round state for the review loop: one activity, one completion record.

A resume restarts the `review-loop` group at `round-pre` (resume.sh:265), and v1
had no way to tell "this round never happened" from "this round finished and the
node output was lost". Five of thirteen review invocations on chain 3460c074
were replays of work that was already done -- ~72 minutes of the run's 306. The
fix is not a smarter node: it is a set of files under `round-N/`, each written
atomically, each naming the exact candidate it was produced for, so every node
can ask "has this activity already completed for THIS candidate?" and answer it
from disk.

Three identities carry the whole protocol:

  review identity  sha256(review_head | base | scope | contract_digest |
                   plan_digest | allowlist_digest). The head, base and scope are
                   the recording's own and never move; the three digests are
                   recomputed fresh every time, so amending a pin, the review
                   contract or the allowlist invalidates a completed review
                   while a fixer commit does not.
  generation       `review.ok.gen`, the attempt whose envelope passed every
                   gate. Downstream authorization is bound to it, so a repair
                   made before a rejection or a failed gate is never reused
                   under the envelope that replaced it.
  tree             the tree the allowlisted worktree content would commit to.
                   Repair identity is the tree, independent of the review id.

Every subcommand takes the artifacts directory first and reads params.json and
round.txt itself; nodes never write these files by hand.

  round-state.py pre <artifacts>              round-pre: advance/reuse decision
  round-state.py gate <artifacts> [--fail R]  review-gate: GATE_5, read-only
                                              guard, ledger merge, review.ok
  round-state.py fix-plan <artifacts>         fix-plan: authorize and decide
  round-state.py commit-fixer <artifacts>     commit-fixer: merge, pin, commit
  round-state.py converge <artifacts>         converge: closure decision table
  round-state.py exit-check <artifacts>       exit-gate: review.ok + fixer.ok
  round-state.py mark <artifacts> <marker> [envelope]
  round-state.py reject-review <artifacts> --reason <text>

`pre` and `fix-plan` print exactly ONE bare JSON line on stdout (a `when:` on a
sibling node reads a JSON field of it and the object must be bare); every human
and typed line from those two goes to stderr, which the node tees into its log.
The other subcommands print their typed lines on stdout.
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Captured before main() redirects: for `pre` and `fix-plan` the ONLY thing that
# may reach real stdout is the JSON line a sibling node's `when:` parses.
REAL_STDOUT = sys.stdout
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
JSON_ONLY = {"pre", "fix-plan"}
READY = ("Ready to merge", "Ready with fixes")
VERDICTS = (*READY, "Not ready")
RUN_REASONS = ("initial", "interrupted", "identity", "head-moved", "gate-failed", "rejected")
# The envelope's trailing fields. Last match wins: review prose quotes earlier
# ones, and the contract puts the authoritative pair at the end.
INPUT_RE = re.compile(r"^[ \t>*_-]*Input:[ \t]*[*_`]*([0-9a-fA-F]{16,})", re.M)
HEAD_RE = re.compile(r"^[ \t>*_-]*Head:[ \t]*[*_`]*([0-9a-fA-F]{7,})", re.M)
VERDICT_RE = re.compile(r"Verdict:\s*[*_`]*\s*(Ready to merge|Ready with fixes|Not ready)")


class Stop(Exception):
    """A typed stop. The message is already the operator-facing line."""


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_text(path, default=""):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return default


def write_atomic(path, text):
    """tmp + rename: a kill mid-write leaves the previous state, never a half file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_json_atomic(path, value):
    return write_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def unlink(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def last(pattern, text):
    found = pattern.findall(text or "")
    return found[-1] if found else ""


def note(*parts):
    print(*parts, file=sys.stderr, flush=True)


def json_line(payload):
    """The one bare JSON line, on real stdout whatever main() redirected."""
    print(json.dumps(payload), file=REAL_STDOUT, flush=True)


def log_activity(rnd, **fields):
    """Append one line to round-N/activity.jsonl.

    This is what item 9's telemetry counts, and it is deliberately a record of
    what was ASKED FOR rather than of what this file decided. A duplicate is
    then derived by the reader -- a `run` whose id or tree a `done` in the same
    round already recorded -- instead of being self-certified here. A check
    whose pass condition is "the code that made the decision agrees with the
    decision" cannot see the decision being wrong.
    """
    path = rnd.rd / "activity.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"round": rnd.n, **fields}, sort_keys=True) + "\n")


class Round:
    """The artifacts directory, its worktree, and the current round's files."""

    def __init__(self, artifacts):
        self.ad = Path(artifacts)
        params = read_json(self.ad / "params.json", {}) or {}
        worktree = params.get("worktree")
        if not isinstance(worktree, str) or not worktree.strip():
            raise Stop(f"ROUND_STATE=FAIL params.json has no worktree [{self.ad}]")
        self.wt = Path(worktree)
        self.n = self._read_round()

    def _read_round(self):
        raw = read_text(self.ad / "round.txt").strip()
        if raw == "":
            return 0
        if not raw.isdigit():
            raise Stop(f"ROUND_STATE=FAIL round.txt is not an integer: [{raw}]")
        return int(raw)

    @property
    def rd(self):
        return self.ad / f"round-{self.n}"

    def prev_rd(self):
        return self.ad / f"round-{self.n - 1}"

    # --- git -------------------------------------------------------------
    def git(self, *args, check=True, env=None):
        proc = subprocess.run(
            ["git", "-C", str(self.wt), *args],
            capture_output=True, encoding="utf-8",
            env=dict(os.environ, **env) if env else None,
        )
        if check and proc.returncode != 0:
            raise Stop(f"ROUND_STATE=FAIL git {' '.join(args)}: {proc.stderr.strip()}")
        return proc

    def out(self, *args):
        return self.git(*args).stdout.strip()

    def ok(self, *args):
        return self.git(*args, check=False).returncode == 0

    def head(self):
        return self.out("rev-parse", "HEAD")

    def head_tree(self):
        return self.out("rev-parse", "HEAD^{tree}")

    # --- the candidate ---------------------------------------------------
    def allowlist(self):
        """The plan's allowlist. Absent is a typed stop, never an empty list.

        T is `git add -A` over these paths, so an empty allowlist makes T equal
        HEAD's tree unconditionally: every repair would read as a no-change
        result, commit-fixer would attest without committing, and the fixer's
        work would be silently dropped with every gate green.
        """
        data = read_json(self.ad / "files-allowlist.json")
        if not isinstance(data, list) or not data:
            raise Stop(f"ROUND_STATE=FAIL files-allowlist.json is missing or empty [{self.ad}]")
        return [p for p in data if isinstance(p, str) and p.strip()]

    def tree(self):
        """T: the tree the allowlisted worktree content would commit to.

        HEAD's tree is read first so unchanged files are present and `T ==
        HEAD^{tree}` holds exactly when nothing allowlisted differs; then
        `git add -A` runs over the allowlist alone, in a throwaway index, so a
        stray outside the plan never moves T and the real index is untouched.
        """
        paths = self.allowlist()
        tracked = set(self.out("ls-tree", "-r", "--name-only", "HEAD").splitlines())
        spec = [p for p in paths if p in tracked or (self.wt / p).exists()]
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
            self.git("read-tree", "HEAD", env=env)
            if spec:
                self.git("add", "-A", "--", *spec, env=env)
            return self.git("write-tree", env=env).stdout.strip()

    def tree_diff_count(self, old, new):
        proc = self.git("diff", "--name-only", old, new, check=False)
        return len([x for x in proc.stdout.splitlines() if x.strip()])

    # --- identity --------------------------------------------------------
    def contract_digest(self):
        path = HERE / "review-contract.md"
        return sha256_bytes(path.read_bytes()) if path.is_file() else EMPTY_SHA

    def plan_digest(self):
        doc = read_json(self.ad / "joint-plan.json")
        return EMPTY_SHA if doc is None else sha256_bytes(canonical(doc))

    def allowlist_digest(self):
        return sha256_bytes(canonical(sorted(self.allowlist())))

    def expected_id(self, record):
        """The id a recorded review must still carry.

        review_head, base and scope are the recording's own -- they describe
        what was reviewed and cannot change retroactively. The three digests are
        read fresh, so a pin amendment, a new review contract or an allowlist
        edit invalidates the completed review, while a fixer commit does not.
        """
        parts = [
            str(record.get("review_head", "")), str(record.get("base", "")),
            str(record.get("scope", "")), self.contract_digest(),
            self.plan_digest(), self.allowlist_digest(),
        ]
        return sha256_bytes("|".join(parts).encode("utf-8"))

    def scope(self):
        for name in ("review-scope.txt", "review-mode.txt"):
            value = read_text(self.rd / name).strip()
            if value:
                return value
        return "full"

    def base(self):
        return read_text(self.rd / "review-base.txt").strip()

    # --- attempts --------------------------------------------------------
    def attempt(self):
        raw = read_text(self.rd / "attempt.txt").strip()
        return int(raw) if raw.isdigit() else 1

    def set_attempt(self, k):
        write_atomic(self.rd / "attempt.txt", f"{k}\n")
        return k


# --- shared predicates ----------------------------------------------------

def fixer_result_sha(rnd):
    path = rnd.rd / "fixer-result.json"
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def fixer_result_valid(rnd):
    checker = HERE / "check-fixer-result.py"
    path = rnd.rd / "fixer-result.json"
    if not checker.is_file() or not path.is_file():
        return False
    proc = subprocess.run([sys.executable, str(checker), str(path)],
                          capture_output=True, encoding="utf-8")
    return proc.returncode == 0


def authorization(rnd):
    """(review_id, generation) when this round's envelope is gated, else None.

    Only `review.ok` authorizes: review-gate writes review-summary.json before
    its final checks, so a readable Ready summary is never authorization.
    """
    if read_text(rnd.rd / "gate.txt").strip() != "PASS":
        return None
    ok = read_json(rnd.rd / "review.ok")
    record = read_json(rnd.rd / "review-input.json")
    if not isinstance(ok, dict) or not isinstance(record, dict):
        return None
    expected = rnd.expected_id(record)
    if ok.get("id") != expected or ok.get("guard") != "PASS":
        return None
    gen = ok.get("gen")
    return (expected, gen) if isinstance(gen, int) else None


def validates(rnd, record, review_id, gen, tree):
    """V(rec): a completion record that describes the state actually present."""
    if not isinstance(record, dict):
        return False
    if record.get("review_id") != review_id or record.get("review_gen") != gen:
        return False
    if not fixer_result_valid(rnd):
        return False
    if record.get("result_sha256") != fixer_result_sha(rnd):
        return False
    return record.get("tree") == tree


def fixer_decision(rnd):
    """The fix-plan decision, recomputed from disk.

    commit-fixer runs under `all_done` and a resume loses node outputs, so both
    nodes derive this from the same files rather than passing it along.
    """
    auth = authorization(rnd)
    if auth is None:
        return {"fixer": "unauthorized"}
    review_id, gen = auth
    tree = rnd.tree()
    fok = read_json(rnd.rd / "fixer.ok")
    repair = read_json(rnd.rd / "repair.json")
    start = read_json(rnd.rd / "repair-start.json")
    if isinstance(fok, dict) and validates(rnd, fok, review_id, gen, tree) \
            and fok.get("head") == rnd.head() and rnd.head_tree() == tree:
        return {"fixer": "reuse-committed"}
    if fok is None and validates(rnd, repair, review_id, gen, tree):
        return {"fixer": "reuse-uncommitted"}
    if isinstance(start, dict) and start.get("review_gen") == gen and repair is None:
        return {"fixer": "run", "reason": "interrupted"}
    for record in (repair, fok):
        if isinstance(record, dict) and record.get("review_id") == review_id \
                and record.get("review_gen") == gen:
            raise Stop(f"FIXER_TREE_DRIFT round={rnd.n}")
    return {"fixer": "run", "reason": "initial"}


# --- the ledger CLI (setup/ledger.py) -------------------------------------

def ledger_py():
    return Path(os.environ.get("ARCHON_LEDGER_PY") or (HERE / "ledger.py"))


def ledger(rnd, command, *args, repo=False, echo=False):
    """setup/ledger.py, on its own positional CLI: <artifacts> <command> …

    Exit 1 is a VERDICT -- closure found an unclosed blocker -- not a failure;
    only exit 2 is LEDGER=FAIL. Nobody propagates the 1: a converge that died
    non-zero having printed only CLOSURE lines reads as an untyped exit to the
    node harness, and row 10 is a progression, not a stop.
    """
    script = ledger_py()
    if not script.is_file():
        raise Stop(f"ROUND_STATE=FAIL ledger helper is missing: {script}")
    argv = [sys.executable, str(script), str(rnd.ad), command, *[str(a) for a in args]]
    if repo:
        argv += ["--repo", str(rnd.wt)]
    proc = subprocess.run(argv, capture_output=True, encoding="utf-8")
    if proc.returncode >= 2:
        raise Stop(f"LEDGER=FAIL round={rnd.n} {proc.stdout.strip()} {proc.stderr.strip()}".strip())
    if proc.stderr.strip():
        note(proc.stderr.rstrip())
    if echo and proc.stdout.strip():
        print(proc.stdout.rstrip(), flush=True)
    return proc.returncode


def ledger_entries(rnd):
    data = read_json(rnd.rd / "ledger.json", [])
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def cross_repo_gate(rnd):
    """main's converge invocation of cross-repo-keys.py, in Python.

    Returns (resolved, lines). exit 0 means every `cross_repo` entry carries a
    human acknowledgement in cross-repo-filed.json, and the lines are one
    CROSS_REPO_ACKED each; exit 1 means some are open, and the LAST line is the
    `count=N repos=a,b` tail main's converge appends to CROSS_REPO_FINDING.

    No CROSS_REPO_KEYS=NONE here: that line belongs to the script's operator
    listing mode, not to --gate, which prints nothing when there are no
    cross_repo entries. This mirrors main's converge hunk exactly.
    """
    script = HERE / "cross-repo-keys.py"
    if not script.is_file():
        return True, []
    proc = subprocess.run(
        [sys.executable, str(script), str(rnd.ad), "--gate", str(rnd.rd / "fixer-result.json")],
        capture_output=True, encoding="utf-8")
    if proc.stderr.strip():
        note(proc.stderr.rstrip())
    return proc.returncode == 0, proc.stdout.rstrip("\n").splitlines() if proc.stdout.strip() else []


def mark_filed_acked(rnd):
    """Record the acknowledgement in the ledger as `filed_acked`.

    Only reached when the gate passed, and the gate passes only when EVERY
    cross_repo entry is acknowledged -- so every `filed` entry is acknowledged
    and no key matching is needed. The state is what makes the closure
    allowance auditable: a P0/P1 filed against another repository is resolved
    here, not closed here, and the ledger says which.
    """
    entries = ledger_entries(rnd)
    changed = False
    for entry in entries:
        if entry.get("state") == "filed":
            entry["state"] = "filed_acked"
            changed = True
    if changed:
        write_json_atomic(rnd.rd / "ledger.json", entries)
    return changed


def ledger_closure(rnd):
    """Closure's verdict, plus the entry facts the decision table needs.

    ledger.py answers one question -- is every P0/P1 closed -- as an exit code
    and a printed line. The table's other inputs (pin_conflict symbols, filed
    entries, open blockers, blockers first seen this round) are read straight
    off round-N/ledger.json, the same file ledger.py just wrote.
    """
    code = ledger(rnd, "closure", rnd.n, echo=True)
    entries = ledger_entries(rnd)
    blocking = [e for e in entries if e.get("severity") in ("P0", "P1")]
    # ledger.py counts any P0/P1 that is not `closed` as unclosed, which is right
    # for every state but one: a finding filed against another repository and
    # acknowledged by a human is resolved, and it can never be closed HERE
    # because the defect is not in this repository. Its exit stays the fast path.
    return {
        "closure_ok": code == 0 or not [e for e in blocking
                                        if e.get("state") not in ("closed", "filed_acked")],
        "pin_conflict": [str(e.get("symbol") or e.get("title") or e.get("id"))
                         for e in entries if e.get("state") == "pin_conflict"],
        "filed": [e for e in entries if e.get("state") == "filed"],
        "blockers_open": sum(1 for e in blocking
                             if e.get("state") in ("open", "applied", "regressed")),
        "new_blockers": sum(1 for e in blocking if str(e.get("round")) == str(rnd.n)),
    }


# --- pre ------------------------------------------------------------------

def open_round(rnd, n, next_mode=None):
    """Open round n: counter, directory, pre-head, attempt 1, ledger copied in."""
    previous = rnd.n
    write_atomic(rnd.ad / "round.txt", f"{n}\n")
    rnd.n = n
    rnd.rd.mkdir(parents=True, exist_ok=True)
    write_atomic(rnd.rd / "pre-head.txt", rnd.head() + "\n")
    rnd.set_attempt(1)
    if previous >= 1:
        # Unconditional: ledger.py falls back to the nearest earlier round that
        # has a ledger. A round that wrote no fixer-result.json writes no
        # ledger either, and a straight copy from it would carry nothing --
        # v1's round 6 did exactly that, and closure at round 7 would then pass
        # on an empty set.
        ledger(rnd, "copy-forward", previous, n, echo=True)
    note(f"ROUND_OPENED round={n} head={rnd.head()} next_mode={next_mode or 'full'}")


def snapshot_review_dirs(rnd):
    """review-gate's baseline for telling THIS round's ce-code-review run
    directory from every other one on the host.

    Taken before the reviewer starts. An empty baseline is still written as a
    file, because a missing one and an empty one mean opposite things to the
    gate's `comm`: empty means "nothing was here", missing means "no idea",
    and reading the second as the first lets a foreign run's verdict through.
    """
    root = Path(os.environ.get("CE_REVIEW_ROOT")
                or "/tmp/compound-engineering/ce-code-review")
    dirs = sorted(f"{p}/" for p in root.glob("*") if p.is_dir()) if root.is_dir() else []
    write_atomic(rnd.rd / "prerun-dirs.txt", "".join(d + "\n" for d in dirs))


def decision_bound(rnd, decision):
    """A decision only speaks for the state it was written against."""
    if not isinstance(decision, dict):
        return False
    record = read_json(rnd.rd / "review-input.json")
    if not isinstance(record, dict):
        return False
    return decision.get("head") == rnd.head() and decision.get("id") == rnd.expected_id(record)


def reconcile_pending_commit(rnd):
    """Step 0: the commit happened, the attestation did not.

    post-fix.json is written immediately before `git commit`. If HEAD's tree is
    that tree and no fixer.ok exists, the commit landed and the marker was lost
    between the two: merge the ledger again (idempotent) and write the
    attestation from the repair record that is already on disk.
    """
    post = read_json(rnd.rd / "post-fix.json")
    if not isinstance(post, dict) or (rnd.rd / "fixer.ok").is_file():
        return
    tree = post.get("tree")
    if not tree or rnd.head_tree() != tree:
        return
    repair = read_json(rnd.rd / "repair.json")
    if not isinstance(repair, dict):
        return
    ledger(rnd, "merge-fixer", rnd.n, rnd.rd / "fixer-result.json",
           repair.get("attempt") or rnd.attempt(), repo=True)
    write_json_atomic(rnd.rd / "fixer.ok", {
        "attempt": repair.get("attempt"), "review_id": repair.get("review_id"),
        "review_gen": repair.get("review_gen"), "result_sha256": repair.get("result_sha256"),
        "tree": tree, "head": rnd.head(), "committed": True,
    })
    note(f"FIXER_RECONCILED round={rnd.n} head={rnd.head()} (commit landed, attestation lost)")


def consume_rejection(rnd, k):
    envelope = rnd.rd / "review-envelope.txt"
    if envelope.is_file():
        envelope.replace(rnd.rd / f"review-envelope.rejected-{k}.txt")
    unlink(rnd.rd / "review-rejected.txt")
    unlink(rnd.rd / "review.ok")


def consult_reclaim(rnd):
    """Consulted only when no COMPLETE envelope exists.

    v1's counter advanced before the work it counted, so a round that died to a
    cost cap was billed and not spent, and round-reclaim.sh gave it back. v2's
    counter advances only on a `progressed` decision, so the over-count it
    existed to undo cannot happen here: the call is kept for its typed line and
    its ledger, and its answer is NOT written back to round.txt. The pre-round
    cap stays where it is, reading the counter this helper never moves.
    """
    reclaim = HERE / "round-reclaim.sh"
    if not reclaim.is_file():
        return
    proc = subprocess.run(
        ["bash", str(reclaim), str(rnd.ad), str(rnd.n), "round-", "review-summary.json",
         r'"verdict"[[:space:]]*:[[:space:]]*"[^"]'],
        capture_output=True, encoding="utf-8")
    if proc.stderr.strip():
        note(proc.stderr.rstrip())
    note(f"ROUND_RECLAIM_CONSULTED round={rnd.n} answer={proc.stdout.strip()}")


def decide_review(rnd, k):
    """Step 3: reuse a completed review, or name why it must run again."""
    record = read_json(rnd.rd / "review-input.json")
    if (rnd.rd / "review-rejected.txt").is_file():
        consume_rejection(rnd, k)
        return "run", "rejected"
    if not isinstance(record, dict):
        return "run", "initial"
    if read_text(rnd.rd / "gate.txt").strip() == "FAIL":
        return "run", "gate-failed"
    envelope = read_text(rnd.rd / "review-envelope.txt")
    if not envelope.strip() or "Review complete" not in envelope \
            or not VERDICT_RE.search(envelope):
        consult_reclaim(rnd)
        return "run", "interrupted"
    if last(INPUT_RE, envelope) != rnd.expected_id(record):
        return "run", "identity"
    if last(HEAD_RE, envelope) != record.get("review_head"):
        return "run", "head-moved"
    allowed = {read_text(rnd.rd / "pre-head.txt").strip()}
    fok = read_json(rnd.rd / "fixer.ok")
    if isinstance(fok, dict) and fok.get("head"):
        allowed.add(fok["head"])
    if rnd.head() not in allowed:
        return "run", "head-moved"
    return "reuse", "complete"


def guard_resnapshot(rnd, record, tree):
    """A reviewer that edited and was interrupted must not bake its edit in.

    The next attempt re-snapshots the tree; if that snapshot differs from the
    previous attempt's and the fixer never took ownership, the edit is the
    reviewer's and the round stops here rather than carrying it forward as
    reviewed material.

    Bound to an unmoved HEAD on purpose. The reviewer cannot commit -- only
    commit-impl and commit-fixer do -- so a tree that moved along with HEAD is
    committed drift, which is `head-moved` here and REVIEW_TREE_DRIFT in
    converge. Firing this guard there would turn every foreign commit into a
    reviewer accusation and hide the drift that actually happened.
    """
    if not isinstance(record, dict):
        return
    previous = record.get("review_tree")
    if not previous or previous == tree:
        return
    if rnd.head() != record.get("review_head"):
        return
    start = read_json(rnd.rd / "repair-start.json")
    if isinstance(start, dict) and start.get("review_gen") == record.get("attempt"):
        return
    files = rnd.tree_diff_count(previous, tree)
    raise Stop(f"REVIEW_WROTE_TREE round={rnd.n} files={files}")


def run_review_mode(rnd):
    """B's review-mode.py owns the mode/base decision; honour its typed stop."""
    script = HERE / "review-mode.py"
    if not script.is_file():
        return
    proc = subprocess.run([sys.executable, str(script), str(rnd.ad), str(rnd.n)],
                          capture_output=True, encoding="utf-8")
    for stream in (proc.stdout, proc.stderr):
        if stream.strip():
            note(stream.rstrip())
    if proc.returncode != 0 or "REVIEW_BASE=FAIL" in (proc.stdout + proc.stderr):
        raise Stop(f"REVIEW_BASE=FAIL round={rnd.n} review-mode.py exit={proc.returncode}")


def validate_base(rnd):
    """Item 6: the chosen base must exist and be an ancestor. No silent fallback."""
    base = rnd.base()
    if not base:
        raise Stop("REVIEW_BASE=FAIL base= reason=missing")
    if not rnd.ok("cat-file", "-e", f"{base}^{{commit}}"):
        raise Stop(f"REVIEW_BASE=FAIL base={base} reason=missing")
    if not rnd.ok("merge-base", "--is-ancestor", base, "HEAD"):
        raise Stop(f"REVIEW_BASE=FAIL base={base} reason=not-ancestor")
    return base


def emit_pre(rnd, review, reason, k):
    record = read_json(rnd.rd / "review-input.json", {}) or {}
    log_activity(rnd, kind="review", decision=review, reason=reason, attempt=k,
                 id=record.get("id", ""))
    note(f"ROUND_REUSE round={rnd.n} review={review} attempt={k} reason={reason}")
    json_line({"round": rnd.n, "review": review, "attempt": k, "reason": reason})


def cmd_pre(rnd, _args):
    if rnd.n == 0:
        open_round(rnd, 1)
    else:
        decision = read_json(rnd.rd / "decision.json")
        if decision_bound(rnd, decision):
            result = decision.get("result")
            if result == "converged":
                k = rnd.set_attempt(rnd.attempt() + 1)
                note(f"ROUND_TERMINAL_REPLAY round={rnd.n} head={rnd.head()}")
                emit_pre(rnd, "reuse", "terminal-replay", k)
                return 0
            if result == "progressed":
                open_round(rnd, rnd.n + 1, decision.get("next_mode"))
            else:
                note(f"ROUND_BLOCKED round={rnd.n} reason={decision.get('reason')} "
                     "(re-evaluating this round)")
                rnd.set_attempt(rnd.attempt() + 1)
        else:
            if isinstance(decision, dict):
                note(f"ROUND_DECISION_UNBOUND round={rnd.n} (head or identity moved; ignored)")
            rnd.set_attempt(rnd.attempt() + 1)
    k = rnd.attempt()
    reconcile_pending_commit(rnd)
    review, reason = decide_review(rnd, k)
    if review == "reuse":
        emit_pre(rnd, review, reason, k)
        return 0
    previous = read_json(rnd.rd / "review-input.json")
    tree = rnd.tree()
    guard_resnapshot(rnd, previous, tree)
    snapshot_review_dirs(rnd)
    run_review_mode(rnd)
    base = validate_base(rnd)
    record = {
        "review_head": rnd.head(), "review_tree": tree, "base": base, "scope": rnd.scope(),
        "contract_digest": rnd.contract_digest(), "plan_digest": rnd.plan_digest(),
        "allowlist_digest": rnd.allowlist_digest(), "attempt": k,
    }
    record["id"] = rnd.expected_id(record)
    write_json_atomic(rnd.rd / "review-input.json", record)
    unlink(rnd.rd / "review.ok")
    write_atomic(rnd.rd / "gate.txt", "PENDING\n")
    note(f"REVIEW_INPUT round={rnd.n} id={record['id']} head={record['review_head']} "
         f"base={base} scope={record['scope']}")
    emit_pre(rnd, review, reason, k)
    return 0


# --- gate -----------------------------------------------------------------

def fail_gate(rnd, line):
    """Every gate failure ends on REVIEW_GATE=FAIL, whatever named it.

    The node prints REVIEW_GATE=PASS itself on the success path, so this helper
    never prints a PASS -- test_node_review_gate asserts exactly one.
    """
    write_atomic(rnd.rd / "gate.txt", "FAIL\n")
    unlink(rnd.rd / "review.ok")
    print(line, flush=True)
    if not line.startswith("REVIEW_GATE=FAIL"):
        print(f"REVIEW_GATE=FAIL round={rnd.n}", flush=True)
    return 1


def cmd_gate(rnd, args):
    if args.fail:
        return fail_gate(rnd, f"REVIEW_GATE=FAIL round={rnd.n} reason={args.fail}")
    record = read_json(rnd.rd / "review-input.json")
    if not isinstance(record, dict):
        return fail_gate(rnd, f"REVIEW_GATE=FAIL round={rnd.n} reason=no-review-input")
    envelope = read_text(rnd.rd / "review-envelope.txt")
    expected = rnd.expected_id(record)
    got_id, got_head = last(INPUT_RE, envelope), last(HEAD_RE, envelope)
    matched = got_id == expected and got_head == record.get("review_head")
    print(f"GATE_5_input_matches={'PASS' if matched else 'FAIL'}", flush=True)
    if not matched:
        return fail_gate(rnd, f"REVIEW_INPUT=FAIL round={rnd.n} expected={expected} "
                              f"envelope_input=[{got_id}] envelope_head=[{got_head}]")
    gen = record.get("attempt")
    tree = rnd.tree()
    start = read_json(rnd.rd / "repair-start.json")
    owned = isinstance(start, dict) and start.get("review_gen") == gen
    if tree != record.get("review_tree") and not owned:
        files = rnd.tree_diff_count(record.get("review_tree"), tree)
        return fail_gate(rnd, f"REVIEW_WROTE_TREE round={rnd.n} files={files}")
    print(f"GUARD_read_only=PASS owner={'fixer' if owned else 'none'}", flush=True)
    # Last, so review-summary.json (which ledger.py rewrites) carries the real
    # residual count rather than the placeholder the node's earlier write left.
    ledger(rnd, "merge-envelope", rnd.n, rnd.rd / "review-envelope.txt", repo=True)
    write_json_atomic(rnd.rd / "review.ok", {"gen": gen, "id": expected, "guard": "PASS"})
    write_atomic(rnd.rd / "gate.txt", "PASS\n")
    print(f"REVIEW_OK round={rnd.n} gen={gen} id={expected}", flush=True)
    return 0


# --- fix-plan -------------------------------------------------------------

def cmd_fix_plan(rnd, _args):
    decision = fixer_decision(rnd)
    # FIX_PLAN is this node's discriminator, so it prints on EVERY branch. The
    # unauthorized branch exits 0 -- unauthorized is a decision, not a node
    # failure; commit-fixer and converge are what stop -- and without this line
    # the only typed word a successful node printed was REVIEW_UNAUTHORIZED,
    # which runner.py reads as a failure word on a zero exit: an untyped exit.
    note(f"FIX_PLAN round={rnd.n} fixer={decision['fixer']} "
         f"reason={decision.get('reason', '-')}")
    if decision["fixer"] == "unauthorized":
        note(f"REVIEW_UNAUTHORIZED round={rnd.n} (no gated envelope for this candidate)")
        log_activity(rnd, kind="fixer", decision="unauthorized", tree="")
    else:
        log_activity(rnd, kind="fixer", decision=decision["fixer"],
                     reason=decision.get("reason", ""), tree=rnd.tree())
    json_line(decision)
    return 0


# --- commit-fixer ---------------------------------------------------------

def run_pin_guard(rnd):
    """The fixer's comparator is the head the REVIEW saw (this round's pre-head),
    not the stage baseline: the implementer may change a pinned symbol the spec
    itself told it to change, and commit-impl already judged that against the
    baseline. What a fixer round must not do is redesign a pinned body after the
    review -- rounds 2-5 of chain 3460c074 were exactly that."""
    guard = HERE / "pin-guard.py"
    baseline = read_text(rnd.rd / "pre-head.txt").strip() or \
        read_text(rnd.ad / "bootstrap-head.txt").strip()
    if not guard.is_file() or not baseline:
        return
    proc = subprocess.run([sys.executable, str(guard), str(rnd.ad), baseline],
                          capture_output=True, encoding="utf-8", cwd=str(rnd.wt))
    for stream in (proc.stdout, proc.stderr):
        if stream.strip():
            print(stream.rstrip(), flush=True)
    if proc.returncode != 0:
        raise Stop(f"PIN_GUARD=FAIL round={rnd.n} (edit left staged for repair)")


def kill_after(boundary):
    """Injection hook: exit immediately after `boundary`'s write, before the next.

    commit-fixer is one call from the node's point of view, so three of the
    plan's kill points -- between the ledger merge and the commit, after
    post-fix.json before the commit, and after the commit before fixer.ok --
    are unreachable from outside it. The harness would otherwise hand-build the
    post-kill state, which tests a state someone invented rather than the one
    this code actually leaves behind. Unset, this does nothing.
    """
    if os.environ.get("ROUND_STATE_KILL_AFTER") == boundary:
        raise SystemExit(f"ROUND_STATE_KILLED after={boundary}")


def cmd_commit_fixer(rnd, _args):
    decision = fixer_decision(rnd)
    if decision["fixer"] == "unauthorized":
        print(f"REVIEW_UNAUTHORIZED round={rnd.n}", flush=True)
        return 1
    auth = authorization(rnd)
    if auth is None:
        print(f"REVIEW_UNAUTHORIZED round={rnd.n}", flush=True)
        return 1
    review_id, gen = auth
    head = rnd.head()
    if decision["fixer"] == "reuse-committed":
        ledger(rnd, "merge-fixer", rnd.n, rnd.rd / "fixer-result.json",
               rnd.attempt(), repo=True)
        print(f"COMMIT_FIXER=OK round={rnd.n} reused=committed sha={head}", flush=True)
        return 0
    tree = rnd.tree()
    repair = read_json(rnd.rd / "repair.json")
    if not validates(rnd, repair, review_id, gen, tree):
        print(f"FIXER_INCOMPLETE round={rnd.n} attempt={rnd.attempt()}", flush=True)
        return 1
    ledger(rnd, "merge-fixer", rnd.n, rnd.rd / "fixer-result.json",
           repair.get("attempt") or rnd.attempt(), repo=True)
    kill_after("ledger-merged")
    run_pin_guard(rnd)
    attestation = {
        "attempt": repair.get("attempt"), "review_id": review_id, "review_gen": gen,
        "result_sha256": repair.get("result_sha256"), "tree": tree,
    }
    if tree == rnd.head_tree():
        # Two ways to reach an unmoved tree, and they attest differently. A real
        # no-change result committed nothing (v1's COMMITTED=NO). A RE-RUN after
        # a lost fixer.ok also lands here -- the commit already made HEAD's tree
        # equal T -- and recording committed:false there tells converge the round
        # never committed when it did. post-fix.json is written immediately
        # before `git commit`, so its tree is what distinguishes them.
        post = read_json(rnd.rd / "post-fix.json")
        committed = isinstance(post, dict) and post.get("tree") == tree
        attestation.update({"head": head, "committed": committed})
        write_json_atomic(rnd.rd / "fixer.ok", attestation)
        print(f"COMMITTED={'YES sha=' + head if committed else 'NO'}", flush=True)
        print(f"COMMIT_FIXER=OK round={rnd.n} "
              f"committed={str(committed).lower()} sha={head}", flush=True)
        return 0
    write_json_atomic(rnd.rd / "post-fix.json", {"tree": tree})
    kill_after("post-fix-written")
    rnd.git("commit", "-m", "fix(review): apply fixer feedback")
    kill_after("committed")
    head = rnd.head()
    attestation.update({"head": head, "committed": True})
    write_json_atomic(rnd.rd / "fixer.ok", attestation)
    print(f"COMMITTED=YES sha={head}", flush=True)
    print(f"COMMIT_FIXER=OK round={rnd.n} committed=true sha={head}", flush=True)
    return 0


# --- converge -------------------------------------------------------------

def write_decision(rnd, result, next_mode, reason, review_id, gen, tree):
    write_json_atomic(rnd.rd / "decision.json", {
        "result": result, "next_mode": next_mode, "reason": reason,
        "head": rnd.head(), "id": review_id, "gen": gen, "tree": tree,
        "attempt": rnd.attempt(),
    })


def fixer_partitions(rnd):
    data = read_json(rnd.rd / "fixer-result.json", {}) or {}
    return data if isinstance(data, dict) else {}


def converge_rows(rnd, closure, result, verdict, envelope_head, cross_repo):
    """Item 3's table, evaluated in order, first match wins."""
    partitions = fixer_partitions(rnd)
    applied = [e for e in (partitions.get("applied") or []) if isinstance(e, dict)]
    ready = verdict in READY
    if closure.get("pin_conflict"):
        symbol = closure["pin_conflict"][0]
        return "blocked", None, f"pin_conflict:{symbol}", f"PIN_CONFLICT round={rnd.n} symbol={symbol}", 1
    if cross_repo and not cross_repo[0]:
        # cross-repo-keys.py already wrote cross-repo-findings.json and put its
        # `count=N repos=a,b` tail on the last line; the rest are ACK problems.
        tail = cross_repo[1][-1] if cross_repo[1] else ""
        for line in cross_repo[1][:-1]:
            print(line, flush=True)
        return "blocked", None, "cross_repo", \
            f"CROSS_REPO_FINDING round={rnd.n} {tail}".rstrip(), 1
    if partitions.get("incomplete"):
        return "progressed", "full", "incomplete", f"ROUND_PROGRESSED round={rnd.n} (incomplete)", 0
    if verdict == "Not ready" and not closure.get("blockers_open"):
        return "blocked", None, "not-ready-without-blocker", \
            f"NOT_READY_WITHOUT_BLOCKER round={rnd.n}", 1
    if rnd.scope() == "verify" and closure.get("new_blockers"):
        return "progressed", "full", "verify-found-new-blocker", \
            f"ROUND_PROGRESSED round={rnd.n} (verify found a new P0/P1)", 0
    if any(e.get("severity") == "P0" for e in applied) or \
            any(e.get("design_expanded") is True for e in applied):
        return "progressed", "full", "design-expanded-or-p0", \
            f"ROUND_PROGRESSED round={rnd.n} (P0 or design-expanding repair)", 0
    if applied:
        return "progressed", "verify", "applied", \
            f"ROUND_PROGRESSED round={rnd.n} (repairs landed; verify next)", 0
    if verdict == "Not ready":
        return "progressed", "full", "open-blocker-no-repair", \
            f"ROUND_PROGRESSED round={rnd.n} (open blocker, no repair)", 0
    if ready and closure.get("closure_ok") and envelope_head == rnd.head():
        return "converged", None, "closed", f"CONVERGED round={rnd.n}", 0
    if ready and closure.get("closure_ok"):
        return "blocked", None, "review-tree-drift", f"REVIEW_TREE_DRIFT round={rnd.n}", 1
    if ready:
        return "progressed", "verify", "unverified-blocker", \
            f"ROUND_PROGRESSED round={rnd.n} (unverified P0/P1)", 0
    return "blocked", None, "unknown-verdict", f"CONVERGE=FAIL unknown verdict [{result}]", 1


def helper(rnd, name, *args, cwd=None):
    """Run a setup/ helper and pass its output through. Returns its exit code."""
    script = HERE / name
    if not script.is_file():
        return 0
    proc = subprocess.run([sys.executable, str(script), *[str(a) for a in args]],
                          capture_output=True, encoding="utf-8", cwd=cwd)
    for stream in (proc.stdout, proc.stderr):
        if stream.strip():
            print(stream.rstrip(), flush=True)
    return proc.returncode


def converge_preconditions(rnd):
    """The four v1 converge checks the decision table does not restate.

    Each earns its place: check-scope is the primary round-scoped breach check
    and the RUNBOOK recipe for SCOPE_BREACH depends on it firing here;
    check-fixer-result covers the `failed` partition and an unreadable result,
    neither of which row 2 sees; update-waivers writes waivers.json, which the
    review and fixer prompts both read for "Previously waived" and which
    nothing else writes; review-yield is the operator's per-round number.

    check-fixer-result runs FIRST, ahead of the attestation check, because
    `V(fixer.ok)` itself requires a valid fixer-result: a `failed` partition
    would otherwise surface as FIXER_TREE_DRIFT, naming the wrong cause.
    """
    result = rnd.rd / "fixer-result.json"
    if helper(rnd, "check-fixer-result.py", result, "--require-finding-id") != 0:
        return f"FIXER_BLOCKED round={rnd.n}"
    return None


def converge_reports(rnd):
    """Scope guard, waiver ledger and the yield line. Returns a stop, or None."""
    base = read_text(rnd.ad / "bootstrap-head.txt").strip()
    if base and helper(rnd, "check-scope.py", rnd.ad / "files-allowlist.json",
                       rnd.wt, base, "--round", rnd.n) != 0:
        return f"SCOPE_BREACH round={rnd.n}"
    helper(rnd, "update-waivers.py", rnd.rd / "fixer-result.json",
           rnd.ad / "waivers.md")
    # Exit code deliberately discarded: review-yield is a report now. Its only
    # consumer was the opt-in yield-stop convergence branch, which closure
    # convergence retired, so a DIMINISHING verdict must not stop the round.
    helper(rnd, "review-yield.py", rnd.ad, rnd.n)
    return None


def round_verdict(rnd):
    """The verdict the NODE typed as GATE_3, not one re-derived here.

    review-summary.json is the compatibility artifact and ledger.py rewrites it;
    review-verdict.txt is what review-gate resolved from the ce run directory.
    Prefer the node's, so converge gates on the value the operator was shown.
    """
    typed = read_text(rnd.rd / "review-verdict.txt").strip()
    if typed in VERDICTS:
        return typed
    return (read_json(rnd.rd / "review-summary.json", {}) or {}).get("verdict")


def cmd_converge(rnd, _args):
    auth = authorization(rnd)
    if auth is None:
        print(f"REVIEW_UNAUTHORIZED round={rnd.n}", flush=True)
        return 1
    review_id, gen = auth
    blocked = converge_preconditions(rnd)
    if blocked:
        print(blocked, flush=True)
        return 1
    tree = rnd.tree()
    fok = read_json(rnd.rd / "fixer.ok")
    if not isinstance(fok, dict):
        print(f"FIXER_ABSENT round={rnd.n}", flush=True)
        return 1
    if not validates(rnd, fok, review_id, gen, tree) or fok.get("head") != rnd.head() \
            or rnd.head_tree() != tree:
        print(f"FIXER_TREE_DRIFT round={rnd.n}", flush=True)
        return 1
    previous = read_json(rnd.rd / "decision.json")
    if decision_bound(rnd, previous) and isinstance(previous, dict) \
            and previous.get("result") == "converged" \
            and previous.get("gen") == gen and previous.get("tree") == tree:
        print(f"CONVERGED round={rnd.n}", flush=True)
        print("<promise>REVIEW_CONVERGED</promise>", flush=True)
        return 0
    blocked = converge_reports(rnd)
    if blocked:
        print(blocked, flush=True)
        return 1
    cross_repo = cross_repo_gate(rnd)
    if cross_repo[0]:
        for line in cross_repo[1]:
            print(line, flush=True)
        mark_filed_acked(rnd)
    closure = ledger_closure(rnd)
    verdict = round_verdict(rnd)
    envelope_head = last(HEAD_RE, read_text(rnd.rd / "review-envelope.txt"))
    result = verdict if verdict in VERDICTS else "UNKNOWN"
    outcome, next_mode, reason, line, code = converge_rows(
        rnd, closure, result, verdict if verdict in VERDICTS else None, envelope_head,
        cross_repo)
    if outcome == "converged":
        ledger(rnd, "residuals", rnd.n, rnd.ad / "residuals.json", echo=True)
    write_decision(rnd, outcome, next_mode, reason, review_id, gen, tree)
    print(line, flush=True)
    if outcome == "converged":
        print("<promise>REVIEW_CONVERGED</promise>", flush=True)
    return code


def cmd_exit_check(rnd, _args):
    """exit-gate's belt: the final round must carry its own authorization.

    exit-gate reads review-summary.json's verdict, which review-gate writes
    BEFORE its final checks and which ledger.py rewrites on every envelope
    merge. A readable Ready verdict therefore proves nothing about whether the
    review was gated or the repair was attested; only review.ok and fixer.ok do,
    and only when they are bound to the candidate that is actually at HEAD.
    """
    auth = authorization(rnd)
    if auth is None:
        print(f"EXIT_GATE=FAIL REVIEW_UNAUTHORIZED round={rnd.n}", flush=True)
        return 1
    review_id, gen = auth
    fok = read_json(rnd.rd / "fixer.ok")
    if not isinstance(fok, dict):
        print(f"EXIT_GATE=FAIL FIXER_ABSENT round={rnd.n}", flush=True)
        return 1
    if fok.get("head") != rnd.head():
        print(f"EXIT_GATE=FAIL FIXER_ABSENT round={rnd.n} "
              f"attested={fok.get('head')} head={rnd.head()}", flush=True)
        return 1
    print(f"EXIT_CHECK=OK round={rnd.n} gen={gen} id={review_id} head={rnd.head()}",
          flush=True)
    return 0


# --- mark / reject --------------------------------------------------------

def cmd_mark(rnd, args):
    marker, k = args.marker, rnd.attempt()
    if marker == "review-start":
        write_json_atomic(rnd.rd / "review-start.json", {"attempt": k})
        print(f"REVIEW_START round={rnd.n} attempt={k}", flush=True)
        return 0
    if marker == "review-done":
        if not args.envelope:
            raise Stop("ROUND_STATE=FAIL mark review-done requires an envelope path")
        text = read_text(args.envelope)
        if not text.strip():
            raise Stop(f"ROUND_STATE=FAIL review envelope is empty: {args.envelope}")
        write_atomic(rnd.rd / "review-envelope.txt", text)
        record = read_json(rnd.rd / "review-input.json", {}) or {}
        log_activity(rnd, kind="review", decision="done", id=record.get("id", ""))
        print(f"REVIEW_ENVELOPE=WRITTEN round={rnd.n} bytes={len(text)}", flush=True)
        return 0
    auth = authorization(rnd)
    if auth is None:
        print(f"REVIEW_UNAUTHORIZED round={rnd.n}", flush=True)
        return 1
    review_id, gen = auth
    if marker == "repair-start":
        write_json_atomic(rnd.rd / "repair-start.json", {"attempt": k, "review_gen": gen})
        print(f"REPAIR_START round={rnd.n} attempt={k} gen={gen}", flush=True)
        return 0
    if marker == "repair-done":
        sha = fixer_result_sha(rnd)
        if sha is None:
            raise Stop(f"FIXER_INCOMPLETE round={rnd.n} attempt={k} (no fixer-result.json)")
        tree = rnd.tree()
        write_json_atomic(rnd.rd / "repair.json", {
            "attempt": k, "review_id": review_id, "review_gen": gen,
            "result_sha256": sha, "tree": tree,
        })
        log_activity(rnd, kind="fixer", decision="done", tree=tree)
        print(f"REPAIR_DONE round={rnd.n} attempt={k} gen={gen}", flush=True)
        return 0
    raise Stop(f"ROUND_STATE=FAIL unknown marker: {marker}")


def cmd_reject_review(rnd, args):
    write_atomic(rnd.rd / "review-rejected.txt", args.reason.strip() + "\n")
    unlink(rnd.rd / "review.ok")
    write_atomic(rnd.rd / "gate.txt", "FAIL\n")
    print(f"REVIEW_REJECTED round={rnd.n} reason={args.reason.strip()}", flush=True)
    return 0


# --- cli ------------------------------------------------------------------

COMMANDS = {
    "pre": cmd_pre, "gate": cmd_gate, "fix-plan": cmd_fix_plan,
    "commit-fixer": cmd_commit_fixer, "converge": cmd_converge,
    "exit-check": cmd_exit_check,
    "mark": cmd_mark, "reject-review": cmd_reject_review,
}


def build_parser():
    parser = argparse.ArgumentParser(prog="round-state.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pre", "fix-plan", "commit-fixer", "converge", "exit-check"):
        sub.add_parser(name).add_argument("artifacts")
    gate = sub.add_parser("gate")
    gate.add_argument("artifacts")
    gate.add_argument("--fail", help="record a gate failure decided by the caller")
    mark = sub.add_parser("mark")
    mark.add_argument("artifacts")
    mark.add_argument("marker", choices=("review-start", "review-done",
                                         "repair-start", "repair-done"))
    mark.add_argument("envelope", nargs="?")
    reject = sub.add_parser("reject-review")
    reject.add_argument("artifacts")
    reject.add_argument("--reason", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    # Every helper this file shells into prints to stdout. For pre and fix-plan
    # that channel belongs to one JSON object, so stdout is redirected wholesale
    # rather than each call site being asked to remember -- a new helper added
    # later cannot reintroduce the leak. Measured: a round-2 transition put
    # ledger.py's LEDGER_COPY line ahead of the JSON and broke the `when:`.
    redirect = contextlib.redirect_stdout(sys.stderr) if args.command in JSON_ONLY \
        else contextlib.nullcontext()
    try:
        with redirect:
            return COMMANDS[args.command](Round(args.artifacts), args)
    except Stop as stop:
        # stdout belongs to the JSON contract for pre and fix-plan; everywhere
        # else the typed stop IS the node's output.
        print(str(stop), file=sys.stderr if args.command in JSON_ONLY else sys.stdout, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
