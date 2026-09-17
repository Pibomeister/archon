#!/usr/bin/env python3
"""Pinned symbols must reach the commit unchanged, or say which exception allows it.

v1's api stage spent rounds 2-5 reviewing code the round-1 fixer had added to
`GroupService.shareGroup`/`reshareGroup` against a spec that said "No other
change to POST /group/share". Nothing enforced that sentence: the planner
carried a paraphrase of it, the reviewer re-derived the objection every round,
and the fixer kept widening the same two methods. A pin is only a pin if a gate
reads it.

This gate reads the BASELINE blob and the STAGED blob -- `git show <sha>:<file>`
and `git show :<file>` -- so it judges exactly what `git commit` is about to
write, not what happens to be in the worktree. An agent that edits a file after
staging cannot slip a pinned symbol past it, and one that stages a change it
then reverts in the worktree is still stopped.

  unchanged                        PIN_OK
  changed, allowed_change: none    PIN_BREACH        (exit 1, edit left staged)
  changed, allowed_change: <text>  PIN_CHANGED       (informational)
  symbol or file not found         PIN_UNRESOLVED    (exit 1)

"Not found" is a typed stop rather than a pass on purpose: a silently missed
span is the worse failure, because it reports a pin as held while nothing
checked it.

Usage: pin-guard.py <artifacts-dir> <baseline-sha>
"""
import json
import re
import subprocess
import sys
from pathlib import Path

# ponytail: line-anchored regex + a brace/indent scanner, not a parser. It reads
# TypeScript methods, functions and arrow properties (decorators and multi-line
# signatures included) and Python `def`. Overload sets, symbols built by a
# factory and anything generated resolve to PIN_UNRESOLVED rather than to a
# wrong span. Upgrade path: tree-sitter (tree-sitter-typescript /
# tree-sitter-python), which gives exact node ranges and removes every case
# below where this file has to guess.
TS_EXTENSIONS = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
WORD = r"[A-Za-z_$][A-Za-z0-9_$]*"


def fail(line):
    print(line)
    raise SystemExit(1)


def git(*args, cwd=None):
    return subprocess.run(["git", *args], capture_output=True, encoding="utf-8", cwd=cwd)


def blob(ref, path, cwd=None):
    """The file's content at a ref, or None when the ref does not carry it."""
    proc = git("show", f"{ref}:{path}", cwd=cwd)
    return proc.stdout if proc.returncode == 0 else None


# --- scanning -------------------------------------------------------------

def strip_code(text):
    """Blank out string and comment bodies so brace matching sees only code.

    Positions are preserved, so an offset into the result indexes the original.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if two == "//":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if two == "/*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
            continue
        if ch in "'\"`":
            quote, j = ch, i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if text[k] != "\n":
                    out[k] = " "
            i = max(j, i + 1)
            continue
        i += 1
    return "".join(out)


def match_pair(code, start, opener, closer):
    """Index just past the closer that balances `code[start] == opener`."""
    depth = 0
    for i in range(start, len(code)):
        if code[i] == opener:
            depth += 1
        elif code[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def ts_definition_end(code, after_name):
    """End offset of a TypeScript definition whose name ends at `after_name`.

    The parameter list is matched first, so a destructured default (`{a = 1}`)
    never reads as the body. Then the return-type region is walked with angle,
    paren and bracket depths, so `: Promise<{ a: string }>` does not either. The
    first `{` at zero depth opens the body; a `;` first means a declaration or
    an expression-bodied arrow, which ends there.
    """
    open_paren = code.find("(", after_name)
    if open_paren < 0:
        return -1
    cursor = match_pair(code, open_paren, "(", ")")
    if cursor < 0:
        return -1
    angle = paren = bracket = 0
    while cursor < len(code):
        ch = code[cursor]
        if ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif angle == paren == bracket == 0:
            if ch == "{":
                return match_pair(code, cursor, "{", "}")
            if ch == ";":
                return cursor + 1
        cursor += 1
    return -1


def ts_header(text, symbol):
    """(match_start, offset_after_name) for the symbol's definition, or None."""
    name = re.escape(symbol)
    patterns = (
        # function / method, with any modifier prefix and optional generics
        rf"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:declare[ \t]+)?"
        rf"(?:public|private|protected|static|readonly|abstract|override|async|function|\*)"
        rf"(?:[ \t]+(?:public|private|protected|static|readonly|abstract|override|async|function|\*))*"
        rf"[ \t]+{name}[ \t]*(?:<[^<>()]*>)?[ \t]*\(",
        rf"^[ \t]*{name}[ \t]*(?:<[^<>()]*>)?[ \t]*\(",
        # arrow / value property: `foo = (…) => …`, `const foo = async (…) => …`
        rf"^[ \t]*(?:export[ \t]+)?(?:const|let|var)?[ \t]*"
        rf"(?:public|private|protected|static|readonly)?[ \t]*"
        rf"(?:public|private|protected|static|readonly)?[ \t]*"
        rf"{name}[ \t]*(?::[^=\n]*)?=[ \t]*(?:async[ \t]*)?(?:<[^<>()]*>)?[ \t]*\(",
    )
    for pattern in patterns:
        found = list(re.finditer(pattern, text, re.M))
        if len(found) == 1:
            return found[0].start(), found[0].end() - 1
        if len(found) > 1:
            # Two definitions of one name: an overload set or a name reused in
            # two classes. Guessing which one the pin meant is how a guard
            # silently checks the wrong span.
            return None
    return None


def decorator_start(text, start):
    """Extend the span up over the decorators attached to this definition.

    A decorator's argument list can span lines, so the walk goes up through any
    line that closes more parens than it opens (`  )` above `@UseGuards(`). That
    alone would also swallow a preceding multi-line call expression, so the
    extension is kept only when an `@` line was actually reached.
    """
    lines = text[:start].splitlines(keepends=True)
    index, pending, saw_decorator = len(lines), 0, False
    while index > 0:
        line = lines[index - 1]
        stripped = line.strip()
        delta = line.count(")") - line.count("(")
        if not (pending > 0 or stripped.startswith("@") or delta > 0):
            break
        saw_decorator = saw_decorator or stripped.startswith("@")
        pending = max(0, pending + delta)
        index -= 1
    if not saw_decorator:
        return start
    return sum(len(x) for x in lines[:index])


def ts_span(text, symbol):
    header = ts_header(text, symbol)
    if header is None:
        return None
    start, after_name = header
    code = strip_code(text)
    end = ts_definition_end(code, after_name)
    if end < 0:
        return None
    return decorator_start(text, start), end


def python_span(text, symbol):
    """Indentation decides the body; decorators above are part of the span."""
    pattern = rf"^([ \t]*)(?:async[ \t]+)?def[ \t]+{re.escape(symbol)}[ \t]*\("
    found = list(re.finditer(pattern, text, re.M))
    if len(found) != 1:
        return None
    match = found[0]
    indent = len(match.group(1).expandtabs(4))
    lines = text.splitlines(keepends=True)
    offsets, cursor = [], 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    header_index = next(i for i, off in enumerate(offsets) if off + len(lines[i]) > match.start())
    end_index = len(lines)
    for i in range(header_index + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        width = len(lines[i][:len(lines[i]) - len(lines[i].lstrip())].expandtabs(4))
        if width <= indent:
            end_index = i
            break
    start = header_index
    while start > 0 and lines[start - 1].strip().startswith("@"):
        start -= 1
    return offsets[start], offsets[end_index] if end_index < len(lines) else cursor


def span_of(path, text, symbol):
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return python_span(text, symbol)
    if suffix in TS_EXTENSIONS:
        return ts_span(text, symbol)
    return ts_span(text, symbol) or python_span(text, symbol)


def body_of(path, text, symbol):
    span = span_of(path, text, symbol)
    if span is None:
        return None, None
    start, end = span
    return text[start:end].replace("\r\n", "\n"), [start, end]


# --- pinned decisions -----------------------------------------------------

def pinned_entries(artifacts, repo):
    """Every pinned decision that applies to this repository.

    The planner writes them at the plan's top level or inside the repository's
    own stage; both shapes are read, and an entry that names another repository
    is skipped rather than checked against this worktree.
    """
    doc = json.loads((artifacts / "joint-plan.json").read_text(encoding="utf-8")) \
        if (artifacts / "joint-plan.json").is_file() else {}
    found = []
    pools = [doc.get("pinned_decisions")]
    stages = doc.get("stages")
    if isinstance(stages, dict):
        pools.extend(body.get("pinned_decisions") for body in stages.values()
                     if isinstance(body, dict))
    elif isinstance(stages, list):
        pools.extend(body.get("pinned_decisions") for body in stages
                     if isinstance(body, dict) and (not repo or body.get("repo") == repo))
    for pool in pools:
        if not isinstance(pool, list):
            continue
        for entry in pool:
            if not isinstance(entry, dict):
                continue
            if repo and isinstance(entry.get("repo"), str) and entry["repo"] != repo:
                continue
            if entry not in found:
                found.append(entry)
    return found


def round_dir(artifacts):
    raw = ""
    if (artifacts / "round.txt").is_file():
        raw = (artifacts / "round.txt").read_text(encoding="utf-8").strip()
    if raw.isdigit() and int(raw) >= 1:
        target = artifacts / f"round-{int(raw)}"
        target.mkdir(parents=True, exist_ok=True)
        return target
    return artifacts


def main(argv):
    if len(argv) != 2:
        fail("PIN_GUARD=FAIL usage: pin-guard.py <artifacts-dir> <baseline-sha>")
    artifacts, baseline = Path(argv[0]), argv[1]
    params = json.loads((artifacts / "params.json").read_text(encoding="utf-8")) \
        if (artifacts / "params.json").is_file() else {}
    worktree = params.get("worktree") or None
    repo = params.get("repo")
    entries = [e for e in pinned_entries(artifacts, repo) if e.get("file")]
    spans, breaches, unresolved, changed = [], [], [], []
    for entry in entries:
        symbol, path = str(entry.get("symbol") or ""), str(entry["file"])
        allowed = entry.get("allowed_change", "none")
        record = {"symbol": symbol, "file": path, "allowed_change": allowed}
        before = blob(baseline, path, cwd=worktree)
        after = blob("", path, cwd=worktree)  # `git show :<file>` -- the index
        if not symbol:
            record["result"] = "PIN_UNRESOLVED"
            record["reason"] = "entry names no symbol"
            unresolved.append(record)
            spans.append(record)
            continue
        if before is None or after is None:
            record["result"] = "PIN_UNRESOLVED"
            record["reason"] = "file missing in baseline" if before is None else "file not staged"
            unresolved.append(record)
            spans.append(record)
            continue
        old_body, old_span = body_of(path, before, symbol)
        new_body, new_span = body_of(path, after, symbol)
        if old_body is None or new_body is None:
            record["result"] = "PIN_UNRESOLVED"
            record["reason"] = "symbol not resolvable in baseline" if old_body is None \
                else "symbol not resolvable in staged blob"
            unresolved.append(record)
            spans.append(record)
            continue
        record.update({"baseline_span": old_span, "staged_span": new_span})
        if old_body == new_body:
            record["result"] = "PIN_OK"
        elif str(allowed or "").strip().lower() in ("", "none"):
            # Normalized on purpose: a planner that writes "None" or " none "
            # would otherwise fall through to PIN_CHANGED, and this guard would
            # fail OPEN on the one value that means the pin is absolute.
            record["result"] = "PIN_BREACH"
            breaches.append(record)
        else:
            record["result"] = "PIN_CHANGED"
            changed.append(record)
        spans.append(record)
    (round_dir(artifacts) / "pin-spans.json").write_text(
        json.dumps(spans, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for record in changed:
        print(f"PIN_CHANGED symbol={record['symbol']} allowed={record['allowed_change']}")
    for record in unresolved:
        print(f"PIN_UNRESOLVED symbol={record['symbol'] or '?'} file={record['file']} "
              f"reason={record['reason']}")
    for record in breaches:
        print(f"PIN_BREACH symbol={record['symbol']} file={record['file']}")
    if breaches or unresolved:
        return 1
    print(f"PIN_OK pins={len(entries)} changed={len(changed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
