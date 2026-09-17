#!/usr/bin/env python3
"""Embed setup/prompts/*.md into the lane YAML's prompt nodes.

Usage: embed-prompts.py [--check]

The engine reads the YAML, so a prompt has to live there; the FILE is what gets
edited and reviewed. This splices one into the other, in place, leaving every
other byte of the lane alone -- unlike derive-lite.py, which re-dumps a whole
generated document. full-sdlc-api.yaml is hand-maintained and a re-dump would
reformat 170KB of it.

THE REVIEW MODE IS NAMED HERE AND NOWHERE ELSE. Flipping the measured winner is
the one line below, then `embed-prompts.py && derive-lite.py api &&
derive-codex.py --all`. `lane-doctrine.py check` fails after the swap and needs
one `update`: the review prompt is shared doctrine with bugfix and
full-sdlc-web, and replacing it on this lane alone drops those lines from three
of the five. That is the deliberate-regeneration path the lock exists to force,
not a workaround for it.

`--check` exits 1 when the YAML and the files disagree, which is what stops an
edited prompt from looking landed while the lane still runs the old text.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARCHON = HERE.parent
# The absolute setup root the lanes address, and what {{SETUP}} renders to.
# package.sh reverse-templates this literal on the way into the payload.
SETUP_LITERAL = "/Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup"

# ---- the one line that flips trio <-> capped -----------------------------
REVIEW_MODE = "ce"
# -------------------------------------------------------------------------

LANE = "full-sdlc-api"
PROMPTS = {
    "review": f"review-{REVIEW_MODE}.md",
}
# review-verify.md is NOT embedded. `verify` is a per-round SCOPE the reviewer
# selects by reading round-N/review-scope.txt, not a node, so its body is read
# from disk by the session that needs it -- the same way every mode already
# reads review-contract.md. It still gets rendered, so the copy on disk has no
# unsubstituted placeholders left in it.
RENDER_ONLY = ("review-verify.md",)

# Prepended to the review prompt whatever the mode is. Every line here is
# mode-INDEPENDENT and belongs to item 1's execution contract rather than to any
# topology, so it lives once here instead of three times in B's files -- where a
# mode swap would silently drop it. The tests that caught exactly that are
# test_node_review_loop_shape and test_v2_prompt_contract.
REVIEW_PREAMBLE = f"""YOUR FIRST ACTION, before reading anything else, is to run exactly:
  python3 {SETUP_LITERAL}/round-state.py mark "$ARTIFACTS_DIR" review-start
YOUR LAST ACTION, after your output is complete, is to save the envelope and run
exactly:
  python3 {SETUP_LITERAL}/round-state.py mark "$ARTIFACTS_DIR" review-done <path to the envelope you wrote>
The envelope on disk is what every downstream gate reads; a review that ends
without that second call is an interrupted review and the next round re-runs it,
so the whole invocation is wasted.

YOU ARE READ-ONLY. Do not edit, create, delete or revert any file in the
worktree, and do not commit. The gate takes a tree snapshot before you start and
compares it after, so an edit made here is not an unreviewed fix that slips
through -- it fails the round.

SCOPE OF THIS ROUND. Read round.txt in the artifacts directory for the current
round number N, then read round-N/review-scope.txt. If it reads `verify`, this
round does not rediscover the candidate: read
{SETUP_LITERAL}/prompts/review-verify.md and follow it instead of everything
below, which is the `full` scope. Absent or `full`, continue here.

"""


def render(name):
    """The prompt file with its placeholders resolved."""
    return (HERE / "prompts" / name).read_text(encoding="utf-8").replace(
        "{{SETUP}}", SETUP_LITERAL).rstrip("\n")


def splice(text, node, body):
    """Replace `node`'s `prompt: |` block, leaving the rest of the file alone.

    Indent is read off the node rather than assumed: `docreview` is top level
    and `review` is a loop-body node four spaces deeper, and hardcoding either
    silently edits the wrong one.
    """
    start = re.search(rf"^(?P<pad> *)- id: {re.escape(node)}$", text, re.M)
    if not start:
        raise SystemExit(f"EMBED_PROMPTS=FAIL no node {node} in {LANE}.yaml")
    pad = start.group("pad") + "  "
    head = re.search(rf"^{pad}prompt: \|\n", text[start.end():], re.M)
    if not head:
        raise SystemExit(f"EMBED_PROMPTS=FAIL node {node} has no `prompt: |` block")
    first = start.end() + head.end()
    # The block ends at the first line that is neither blank nor indented past
    # the block header.
    body_pad = pad + "  "
    end = first
    for line in text[first:].splitlines(keepends=True):
        if line.strip() and not line.startswith(body_pad):
            break
        end += len(line)
    indented = "".join(
        ((body_pad + l) if l.strip() else "") + "\n" for l in body.splitlines())
    return text[:first] + indented + text[end:]


def main():
    check = "--check" in sys.argv[1:]
    path = ARCHON / "workflows" / f"{LANE}.yaml"
    text = original = path.read_text(encoding="utf-8")
    for node, name in PROMPTS.items():
        body = render(name)
        if node == "review":
            body = REVIEW_PREAMBLE + body
        text = splice(text, node, body)
    # Rendered IN PLACE, because this one is read off disk at runtime by the
    # session that needs it and a `{{SETUP}}` reaching the model is just literal
    # text it cannot resolve. Idempotent: rendering an already-rendered file is a
    # no-op, and the literal is the same absolute root every other path in this
    # repo uses, which package.sh reverse-templates on the way out.
    for name in RENDER_ONLY:
        target = HERE / "prompts" / name
        rendered = render(name) + "\n"
        if check:
            if target.read_text(encoding="utf-8") != rendered:
                print(f"PROMPT_DRIFT=FAIL {name} has unrendered placeholders"
                      " (regenerate: python3 setup/embed-prompts.py)")
                return 1
        elif target.read_text(encoding="utf-8") != rendered:
            target.write_text(rendered, encoding="utf-8")
    if check:
        if text != original:
            print(f"PROMPT_DRIFT=FAIL {LANE} (regenerate: python3 setup/embed-prompts.py)")
            return 1
        print(f"PROMPT_DRIFT=OK {LANE} mode={REVIEW_MODE}")
        return 0
    path.write_text(text, encoding="utf-8")
    print(f"EMBED_PROMPTS=OK {LANE} mode={REVIEW_MODE} nodes={','.join(PROMPTS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
