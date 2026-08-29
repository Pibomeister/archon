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
    body = body.replace(HARDCODED_ROOTS[0], goodword_root + "/.archon")
    body = body.replace(HARDCODED_ROOTS[1], goodword_root)
    for name, val in (outputs or {}).items():
        quoted = "'" + str(val).replace("'", "'\\''") + "'"
        body = re.sub(r"\$" + re.escape(name) + r"\.output\b", lambda _m: quoted, body)
    leftover = re.findall(r"\$[A-Za-z_][A-Za-z0-9_-]*\.output\b", body)
    if leftover:
        raise ValueError(f"unsubstituted template refs in {workflow}:{node_id}: {sorted(set(leftover))}")
    return body
