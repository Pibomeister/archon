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
from __future__ import annotations
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

# A `/` starts a regex literal only where a value may begin. After an operand
# it is division. This is the standard lexer heuristic, and it is why the list
# is of PRECEDING tokens rather than of following ones.
REGEX_PRECEDERS = ("(", ",", "=", ":", "[", "!", "?", "{", "}", ";", "&", "|",
                   "+", "-", "*", "%", "^", "~", "<", ">")
REGEX_KEYWORDS = ("return", "typeof", "instanceof", "in", "of", "new", "delete",
                  "void", "throw", "case", "do", "else", "yield", "await")


def starts_regex(text, i):
    """True when the `/` at `i` opens a regex literal rather than dividing."""
    j = i - 1
    while j >= 0 and text[j] in " \t\n\r":
        j -= 1
    if j < 0:
        return True
    if text[j] in REGEX_PRECEDERS:
        return True
    k = j
    while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
        k -= 1
    return text[k + 1:j + 1] in REGEX_KEYWORDS


def strip_code(text):
    """Blank out string, comment and regex bodies so brace matching sees code.

    Positions are preserved, so an offset into the result indexes the original.
    A regex literal is the case that bit: `a.replace(/}/g, \'\')` carries an
    unbalanced `}` that walked match_pair off the end of the definition, the
    span truncated, and an edited pinned body compared equal and reported
    PIN_OK -- the one outcome this guard must never produce by accident.
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
        if ch == "/" and starts_regex(text, i):
            j = i + 1
            while j < n and text[j] not in "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":            # a class may hold an unescaped /
                    while j < n and text[j] not in "]\n":
                        j += 2 if text[j] == "\\" else 1
                if text[j] == "/":
                    j += 1
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if text[k] != "\n":
                    out[k] = " "
            i = max(j, i + 1)
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
    """Index just past the closer that balances `code[start] == opener`, or -1.

    -1 propagates all the way to PIN_UNRESOLVED. A truncated span is worse than
    no span: it compares two half-bodies and can report PIN_OK on a real breach.
    """
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
    """(match_start, offset_after_name) for the symbol's definition, or None.

    Matched against the COMMENT-STRIPPED source, so an old definition left in a
    block comment above the live one cannot win the search and silently pin the
    wrong body. Offsets still index the original text.
    """
    text = strip_code(text)
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
    line that closes more parens than it opens (`  )` above `@UseGuards(`). Each
    balanced group is then kept only when the line that OPENED it is itself a
    decorator; otherwise it is an unrelated multi-line call sitting above the
    decorator run, and everything above stays out of the span.
    """
    lines = text[:start].splitlines(keepends=True)
    index, pending, safe = len(lines), 0, len(lines)
    while index > 0:
        stripped = lines[index - 1].strip()
        delta = lines[index - 1].count(")") - lines[index - 1].count("(")
        if pending == 0 and not stripped.startswith("@") and delta <= 0:
            break
        pending = max(0, pending + delta)
        index -= 1
        if pending == 0:
            if not lines[index].strip().startswith("@"):
                break
            safe = index
    return sum(len(x) for x in lines[:safe])


def ts_block_end(code, after_name):
    """End offset of a brace-delimited declaration (class, interface, enum) whose
    name ends at `after_name`: the first `{` outside `extends Foo<Bar>` generics
    opens the body."""
    angle = 0
    cursor = after_name
    while cursor < len(code):
        ch = code[cursor]
        if ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif angle == 0 and ch == "{":
            return match_pair(code, cursor, "{", "}")
        elif angle == 0 and ch == ";":
            return -1
        cursor += 1
    return -1


def ts_class_span(text, symbol):
    """(start, end) of a class / interface / enum declaration, or None."""
    code = strip_code(text)
    pattern = (rf"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:declare[ \t]+)?(?:abstract[ \t]+)?"
               rf"(?:class|interface|enum)[ \t]+{re.escape(symbol)}\b")
    found = list(re.finditer(pattern, code, re.M))
    if len(found) != 1:
        return None
    end = ts_block_end(code, found[0].end())
    if end < 0:
        return None
    return decorator_start(text, found[0].start()), end


ROUTE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)[ \t]+(/\S*)$", re.I)
ROUTE_DECORATOR = re.compile(r"@(Get|Post|Put|Patch|Delete|Head|Options|All)\(\s*(?:['\"`]([^'\"`]*)['\"`])?\s*\)")
CONTROLLER = re.compile(r"@Controller\(\s*(?:['\"`]([^'\"`]*)['\"`])?")
METHOD_HEADER = re.compile(r"^[ \t]*(?:(?:public|private|protected|static|async|override)[ \t]+)*"
                           r"([A-Za-z_$][\w$]*)[ \t]*(?:<[^<>()]*>)?[ \t]*\(", re.M)


def route_path(*parts):
    joined = "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return joined.lower()


def route_span(text, verb, path):
    """The handler method a NestJS route decorator names, decorators included.

    The pin names the route the spec froze (`GET /group/:id/share-link`); the
    body that implements it is the method under the `@Get(...)` whose path,
    joined to the controller prefix, equals the route. Exactly one match or None.
    """
    # Decorator paths are string literals, which strip_code blanks; the raw text
    # is searched for them and the stripped code (same offsets) for the method.
    code = strip_code(text)
    prefix = ""
    controller = CONTROLLER.search(text)
    if controller:
        prefix = controller.group(1) or ""
    hits = [m for m in ROUTE_DECORATOR.finditer(text)
            if (m.group(1).upper() == verb.upper() or m.group(1) == "All")
            and route_path(prefix, m.group(2) or "") == route_path(path)]
    if len(hits) != 1:
        return None
    header = METHOD_HEADER.search(code, hits[0].end())
    if header is None:
        return None
    end = ts_definition_end(code, header.end() - 1)
    if end < 0:
        return None
    return decorator_start(text, header.start()), end


def ts_span(text, symbol):
    route = ROUTE.match(symbol.strip())
    if route:
        return route_span(text, route.group(1), route.group(2))
    if "." in symbol and not symbol.startswith(".") and not symbol.endswith("."):
        # `Class.member`: the member inside that class's own body, so a name
        # reused in another class cannot be the one compared.
        owner, member = symbol.rsplit(".", 1)
        owner_span = ts_class_span(text, owner)
        if owner_span is not None:
            start, end = owner_span
            inner = ts_span(text[start:end], member)
            if inner is None:
                return None
            return start + inner[0], start + inner[1]
        return ts_span(text, member)
    header = ts_header(text, symbol)
    if header is None:
        return ts_class_span(text, symbol)
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


def file_pin(path, symbol):
    """A pin whose symbol is the file itself (the spec's `## Interface (pinned)`
    lists files; the planner copies the path as the symbol)."""
    return symbol.strip() in (path, Path(path).name)


def literal_named(text, symbol):
    """The symbol appears only as a quoted name (an MCP tool name, a registration
    key), not as a declaration the extractor can span."""
    return re.search(r"['\"`]" + re.escape(symbol.strip()) + r"['\"`]", text) is not None


def span_of(path, text, symbol):
    if file_pin(path, symbol):
        return 0, len(text)
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
        # Only THIS repository's stage pool: the api stage must not check the
        # mcp stage's pins against its own worktree (run 9fd801f3 did, and every
        # one of them was "file missing in baseline").
        pools.extend(body.get("pinned_decisions") for name, body in stages.items()
                     if isinstance(body, dict) and (not repo or name == repo))
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


def stage_files(artifacts, repo):
    """(this stage's allowlist, every OTHER stage's allowlist) from the joint plan
    plus the stage's own files-allowlist.json. A top-level pin whose file is in
    another stage's allowlist and not in this one belongs to that repository."""
    doc = json.loads((artifacts / "joint-plan.json").read_text(encoding="utf-8")) \
        if (artifacts / "joint-plan.json").is_file() else {}
    mine, others = set(), set()
    own = artifacts / "files-allowlist.json"
    if own.is_file():
        data = json.loads(own.read_text(encoding="utf-8"))
        mine.update(p for p in data if isinstance(p, str)) if isinstance(data, list) else None
    stages = doc.get("stages")
    if isinstance(stages, dict):
        for name, body in stages.items():
            if not isinstance(body, dict):
                continue
            files = body.get("files_allowlist")
            if not isinstance(files, list):
                continue
            (mine if name == repo else others).update(p for p in files if isinstance(p, str))
    return mine, others


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
    mine, others = stage_files(artifacts, repo)
    spans, breaches, unresolved, changed, informational = [], [], [], [], []

    def note(record, result, reason):
        record["result"], record["reason"] = result, reason
        (unresolved if result == "PIN_UNRESOLVED" else informational).append(record)
        spans.append(record)

    for entry in entries:
        symbol, path = str(entry.get("symbol") or ""), str(entry["file"])
        allowed = entry.get("allowed_change", "none")
        record = {"symbol": symbol, "file": path, "allowed_change": allowed}
        if not symbol:
            note(record, "PIN_UNRESOLVED", "entry names no symbol")
            continue
        if path in others and path not in mine:
            note(record, "PIN_OTHER_REPO", "file belongs to another stage's allowlist")
            continue
        before = blob(baseline, path, cwd=worktree)
        after = blob("", path, cwd=worktree)  # `git show :<file>` -- the index
        if after is None:
            note(record, "PIN_UNRESOLVED",
                 "file missing in baseline and index" if before is None else "file not staged")
            continue
        if file_pin(path, symbol) and path in mine:
            # A whole-file pin on a file the plan itself changes cannot be a
            # freeze; the per-symbol entries carry what is frozen. Outside the
            # allowlist a file pin IS a freeze and is compared whole below.
            note(record, "PIN_UNENFORCEABLE", "file-level pin on a file the plan changes")
            continue
        new_body, new_span = body_of(path, after, symbol)
        if before is None:
            if new_body is not None:
                record["staged_span"] = new_span
                note(record, "PIN_NEW", "introduced by this change (file absent in baseline)")
            elif literal_named(after, symbol):
                note(record, "PIN_UNENFORCEABLE", "named only as a quoted literal, no declaration to span")
            else:
                note(record, "PIN_UNRESOLVED", "symbol not resolvable in staged blob")
            continue
        old_body, old_span = body_of(path, before, symbol)
        if old_body is None and new_body is not None:
            record["staged_span"] = new_span
            note(record, "PIN_NEW", "introduced by this change (absent in baseline)")
            continue
        if old_body is None and new_body is None:
            if literal_named(after, symbol) or literal_named(before, symbol):
                note(record, "PIN_UNENFORCEABLE", "named only as a quoted literal, no declaration to span")
            else:
                note(record, "PIN_UNRESOLVED", "symbol not resolvable in baseline")
            continue
        if new_body is None:
            # Present in the baseline, gone from the staged blob: renamed or
            # deleted. A stop, never a pass -- that is the breach the extractor
            # cannot see.
            note(record, "PIN_UNRESOLVED", "symbol not resolvable in staged blob")
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
    for record in informational:
        if record["result"] != "PIN_OTHER_REPO":
            print(f"{record['result']} symbol={record['symbol']} file={record['file']} reason={record['reason']}")
    for record in breaches:
        print(f"PIN_BREACH symbol={record['symbol']} file={record['file']}")
    if breaches or unresolved:
        return 1
    print(f"PIN_OK pins={len(entries)} changed={len(changed)} "
          f"new={sum(1 for r in informational if r['result'] == 'PIN_NEW')} "
          f"unenforceable={sum(1 for r in informational if r['result'] == 'PIN_UNENFORCEABLE')} "
          f"other_repo={sum(1 for r in informational if r['result'] == 'PIN_OTHER_REPO')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
