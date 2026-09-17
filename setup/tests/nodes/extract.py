"""Extract a bash node body from an Archon workflow YAML and make it runnable
outside the engine: hardcoded absolute roots are rewritten to this checkout and
`$<node>.output` template references are substituted with caller-supplied text."""
import re
from pathlib import Path

import yaml

ARCHON_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ARCHON_ROOT / "workflows"
# The two literal roots baked into the YAML bodies (see package.sh reverse check).
HARDCODED_ROOTS = (
    "/Users/eduardopicazo/Documents/Workspace/Goodword/.archon",
    "/Users/eduardopicazo/Documents/Workspace/Goodword",
)


def _walk(nodes):
    for n in nodes:
        yield n
        if "loop_group" in n:
            yield from _walk(n["loop_group"]["nodes"])


def assert_self_consistent(body, workflow, node_id):
    """A rewritten body must not reach into any checkout but its own.

    This is the one failure mode of running the suite outside the main checkout,
    and it is invisible: the run does not error, it just tests the OTHER tree's
    setup/ scripts and reports their state as this tree's. Fail loudly and name
    the leak instead.
    """
    # Only SIBLING CHECKOUTS count. `$HOME/.archon` is the archon CLI's runtime
    # home -- archon.db, control/, workspaces/, the gitnexus index -- and nodes
    # address it legitimately; flagging it made 30 review-gate tests fail on a
    # tree whose own suite was green. A foreign checkout is one that lives beside
    # this one under the same parent.
    sibling = re.escape(str(ARCHON_ROOT.parent)) + r"/\.archon(?:[A-Za-z0-9._-]*)?/"
    leaked = sorted(set(re.findall(sibling, body)))
    foreign = [x for x in leaked if not x.startswith(str(ARCHON_ROOT) + "/")]
    if foreign:
        raise AssertionError(
            f"NODE_ROOT=LEAK {workflow}:{node_id} addresses a checkout that is not the one "
            f"under test: {foreign}. This checkout is {ARCHON_ROOT}. A run in this state is a "
            "hybrid -- this tree's YAML against another tree's setup/ scripts -- so its "
            "results describe neither. See RUNBOOK 4."
        )
    return body


def node_body(workflow, node_id):
    doc = yaml.safe_load((WORKFLOWS / f"{workflow}.yaml").read_text(encoding="utf-8"))
    for n in _walk(doc["nodes"]):
        if n["id"] == node_id:
            if "bash" not in n:
                raise KeyError(f"{workflow}:{node_id} is not a bash node")
            return n["bash"]
    raise KeyError(f"{workflow}:{node_id} not found")


def runnable_body(workflow, node_id, outputs=None, root=None):
    """Return the node body with roots rewritten to `root` (default: the checkout
    that owns this file) and `$name.output` references replaced from `outputs`.
    Output values are inserted single-quoted so the body's `OUT=$x.output` line
    assigns them verbatim."""
    body = node_body(workflow, node_id)
    goodword_root = str(Path(root) if root else ARCHON_ROOT.parent)
    # The archon root is THIS CHECKOUT, not "<goodword>/.archon". Deriving it as
    # goodword_root + "/.archon" silently sent every worktree back to the real
    # .archon: the body's YAML came from the worktree while the setup/ scripts it
    # shelled into came from the main checkout. A run in that state is a hybrid of
    # two trees, and it fails or passes on code that is not under test. Measured
    # 2026-09-08: four node_stress tests "failed" in a worktree at a commit whose
    # own suite was green, because the main checkout held another session's
    # in-progress browser-evidence digest check.
    archon_root = str(Path(root) / ".archon") if root else str(ARCHON_ROOT)
    body = body.replace(HARDCODED_ROOTS[0], archon_root)
    body = body.replace(HARDCODED_ROOTS[1], goodword_root)
    # Same leak through one level of indirection: bugfix bodies set
    # ROOT="<goodword>" and call "$ROOT/.archon/setup/...". A checkout that is not
    # literally <goodword>/.archon then ran the MAIN checkout's scripts (observed:
    # drill_deslop exercising main's check-slop.py against this tree's YAML).
    # Every ROOT= in the workflows is the goodword root, so the rewrite is exact.
    body = body.replace("$ROOT/.archon", archon_root)
    for name, val in (outputs or {}).items():
        quoted = "'" + str(val).replace("'", "'\\''") + "'"
        body = re.sub(r"\$" + re.escape(name) + r"\.output\b", lambda _m: quoted, body)
    assert_self_consistent(body, workflow, node_id)
    leftover = re.findall(r"\$[A-Za-z_][A-Za-z0-9_-]*\.output\b", body)
    if leftover:
        raise ValueError(f"unsubstituted template refs in {workflow}:{node_id}: {sorted(set(leftover))}")
    return body
