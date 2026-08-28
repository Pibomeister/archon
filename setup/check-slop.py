#!/usr/bin/env python3
"""Delta-only AI-slop scanner for the deslop gate. Scans changed/added lines
between the BASE TREE (`git show <base>:<path>`) and the CURRENT WORKING TREE,
untracked files included — NEVER `base..HEAD` (the api lane's implementation
is uncommitted until commit-impl, so a HEAD-anchored diff would see nothing).
Four guards, each delta-only (pre-existing debt is reported, never blocking):
  complexity        new function >threshold, or an existing function whose
                     complexity crossed the threshold upward (base<=T<current).
                     A function already over threshold in the base tree that
                     is still over threshold is unchanged legacy excess: REPORT.
  tautological_test  added lines of *.spec.ts/*.test.ts: expect(X).toBe(X),
                     expect(true).toBe(true), a newly-added it()/test() block
                     with zero expect(), or one whose only matchers are
                     toBeDefined/toBeTruthy/not.toThrow, or a sole
                     toHaveBeenCalled() assertion.
  narrating_comment  an added `//` comment whose lowercase word-tokens are a
                     subset of the next code line's tokens; banner comments;
                     an added TODO/FIXME/XXX.
  yagni              an added `export` identifier with zero references
                     outside the file that defines it.
Usage: check-slop.py <worktree> <base-sha> [--max-complexity N] [--exclude <path> ...]
Prints one line per finding: `SLOP=FAIL <guard> file=<f> line=<n> ...` (blocking)
or `SLOP=REPORT <guard> ...` (non-blocking), then a final summary line:
`SLOP=OK files=N` (exit 0) or `SLOP=FAIL count=N` (exit 1)."""
import difflib
import re
import subprocess
import sys
from pathlib import Path

BRANCH_RE = re.compile(r"\bif\b|\bfor\b|\bwhile\b|\bcase\b|\bcatch\b|&&|\|\||\?(?!\.|\?)")
FUNC_DECL_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(")
ARROW_CONST_RE = re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]+)?=>")
METHOD_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*"
    r"([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::[^{]+)?\{\s*$"
)
CONTROL_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "else", "do", "try", "finally"}

TAUT_SAME_RE = re.compile(r"expect\(\s*([A-Za-z_$][\w$.]*)\s*\)\.toBe\(\s*\1\s*\)")
TAUT_TRUE_RE = re.compile(r"expect\(\s*true\s*\)\.toBe\(\s*true\s*\)")
BLOCK_START_RE = re.compile(r"^\s*(?:it|test)\s*\(")
MATCHER_RE = re.compile(r"\.(?:not\.)?(\w+)\(")
WEAK_MATCHERS = {"toBeDefined", "toBeTruthy", "toThrow"}

COMMENT_RE = re.compile(r"^\s*//\s*(.*)$")
BANNER_RE = re.compile(r"^\s*//\s*([-=]{3,}|imports?:?|helpers?:?|constants?:?|types?:?|utils?:?|section:?)\s*$", re.I)
TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b")
WORD_RE = re.compile(r"[a-z]{3,}")
# A narrating comment often spells out in English what a terse operator
# already says (e.g. "// increment counter" above "counter++"); credit the
# code line with the words its operators imply so the subset check still
# catches the classic idiom without requiring the comment's exact spelling.
OPERATOR_SYNONYMS = (
    (re.compile(r"\+\+"), {"increment", "increase"}),
    (re.compile(r"--"), {"decrement", "decrease"}),
    (re.compile(r"\+="), {"increment", "increase", "add"}),
    (re.compile(r"-="), {"decrement", "decrease", "subtract"}),
)

EXPORT_DECL_RE = re.compile(r"^\s*export\s+(?:const|function|class|interface|type)\s+(\w+)")
EXPORT_NAMED_RE = re.compile(r"^\s*export\s*\{([^}]+)\}")
EXCLUDE_DIRS = ("node_modules", "dist", ".worktrees")


def git(worktree, *cmd):
    return subprocess.run(
        ["git", "-C", worktree, *cmd], capture_output=True, encoding="utf-8", check=True
    ).stdout


def git_show(worktree, rev, path):
    r = subprocess.run(["git", "-C", worktree, "show", f"{rev}:{path}"], capture_output=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else None


def changed_ts_files(worktree, base, excludes):
    # -z on BOTH commands, not just one. Without it git applies C-style quoting to
    # any path it considers unusual — "a/has space.ts" comes back with the quotes
    # attached, so the .ts suffix test below fails and the file is silently dropped
    # from the slop scan. A gate that skips a file is worse than one that errors.
    # -z also removes the " -> " rename ambiguity: with -z, porcelain emits a
    # rename as two records, the NEW path first and the ORIGINAL second, so the
    # original is consumed and discarded rather than parsed out of one line.
    changed = set()
    for path in git(worktree, "diff", "--name-only", "-z", base).split("\0"):
        if path:
            changed.add(path)
    records = [r for r in git(worktree, "status", "--porcelain", "-z").split("\0") if r]
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4:
            continue
        status, path = rec[:2], rec[3:]
        if "R" in status or "C" in status:
            i += 1  # the following record is the ORIGINAL path; the new path is rec
        if path:
            changed.add(path)
    return sorted(p for p in changed if p not in excludes and (p.endswith(".ts") or p.endswith(".tsx")))


def added_lines(base_text, cur_lines):
    base_lines = base_text.splitlines() if base_text is not None else []
    sm = difflib.SequenceMatcher(None, base_lines, cur_lines, autojunk=False)
    added = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            for k in range(j1, j2):
                added.append((k + 1, cur_lines[k]))
    return added


def complexity(body_text):
    return len(BRANCH_RE.findall(body_text))


def find_functions(lines):
    """name -> (1-indexed start line, body text), via brace-depth matching."""
    funcs = {}
    n = len(lines)
    i = 0
    while i < n:
        name = None
        for rx in (FUNC_DECL_RE, ARROW_CONST_RE):
            m = rx.match(lines[i])
            if m:
                name = m.group(1)
                break
        if name is None:
            m = METHOD_RE.match(lines[i])
            if m and m.group(1) not in CONTROL_KEYWORDS:
                name = m.group(1)
        if name:
            depth, found_open, j, body = 0, False, i, []
            while j < n:
                for ch in lines[j]:
                    if ch == "{":
                        depth += 1
                        found_open = True
                    elif ch == "}":
                        depth -= 1
                body.append(lines[j])
                if found_open and depth <= 0:
                    break
                j += 1
            if found_open:
                funcs[name] = (i + 1, "\n".join(body))
                i = j
        i += 1
    return funcs


def check_complexity(file, base_text, cur_lines, threshold, fails, reports):
    base_lines = base_text.splitlines() if base_text is not None else []
    base_funcs = find_functions(base_lines) if base_text is not None else {}
    cur_funcs = find_functions(cur_lines)
    for name, (lineno, body) in cur_funcs.items():
        val = complexity(body)
        if val <= threshold:
            continue
        if name in base_funcs:
            base_val = complexity(base_funcs[name][1])
            if base_val <= threshold:
                fails.append(f"SLOP=FAIL complexity file={file} line={lineno} fn={name} value={val} threshold={threshold}")
            else:
                reports.append(f"SLOP=REPORT complexity fn={name} file={file} value={val}")
        else:
            fails.append(f"SLOP=FAIL complexity file={file} line={lineno} fn={name} value={val} threshold={threshold} new=true")


def extract_block(lines, start_idx):
    depth, found, j, n, body = 0, False, start_idx, len(lines), []
    while j < n:
        for ch in lines[j]:
            if ch == "{":
                depth += 1
                found = True
            elif ch == "}":
                depth -= 1
        body.append(lines[j])
        if found and depth <= 0:
            return body
        j += 1
    return None


def check_tautological(file, added, cur_lines, fails):
    for no, text in added:
        if TAUT_SAME_RE.search(text) or TAUT_TRUE_RE.search(text):
            fails.append(f"SLOP=FAIL tautological_test file={file} line={no} reason=same-expression")
    for no, text in added:
        if not BLOCK_START_RE.match(text):
            continue
        block = extract_block(cur_lines, no - 1)
        if block is None:
            continue
        block_text = "\n".join(block)
        expect_count = block_text.count("expect(")
        if expect_count == 0:
            fails.append(f"SLOP=FAIL tautological_test file={file} line={no} reason=zero-assertions")
            continue
        matchers = {m for m in MATCHER_RE.findall(block_text) if m != "not"}
        if matchers and matchers <= WEAK_MATCHERS:
            fails.append(f"SLOP=FAIL tautological_test file={file} line={no} reason=weak-matchers-only")
        elif matchers == {"toHaveBeenCalled"} and expect_count == 1:
            fails.append(f"SLOP=FAIL tautological_test file={file} line={no} reason=sole-toHaveBeenCalled")


def check_comments(file, added, cur_lines, fails):
    for no, text in added:
        if BANNER_RE.match(text):
            fails.append(f"SLOP=FAIL narrating_comment file={file} line={no} reason=banner")
            continue
        m = COMMENT_RE.match(text)
        if not m:
            continue
        comment_body = m.group(1)
        if TODO_RE.search(comment_body):
            fails.append(f"SLOP=FAIL narrating_comment file={file} line={no} reason=todo")
            continue
        next_line = None
        k = no
        while k < len(cur_lines):
            if cur_lines[k].strip():
                next_line = cur_lines[k]
                break
            k += 1
        if next_line is None:
            continue
        comment_tokens = set(WORD_RE.findall(comment_body.lower()))
        code_tokens = set(WORD_RE.findall(next_line.lower()))
        for rx, synonyms in OPERATOR_SYNONYMS:
            if rx.search(next_line):
                code_tokens |= synonyms
        if comment_tokens and comment_tokens <= code_tokens:
            fails.append(f"SLOP=FAIL narrating_comment file={file} line={no} reason=restates-code")


def check_yagni(worktree, file, added, fails):
    names = []
    for no, text in added:
        m = EXPORT_DECL_RE.match(text)
        if m:
            names.append((no, m.group(1)))
            continue
        m = EXPORT_NAMED_RE.match(text)
        if m:
            for part in m.group(1).split(","):
                part = part.strip().split(" as ")[0].strip()
                if part:
                    names.append((no, part))
    target = (Path(worktree) / file).resolve()
    for no, name in names:
        cmd = ["grep", "-rlw", name, worktree, "--include=*.ts", "--include=*.tsx"]
        for d in EXCLUDE_DIRS:
            cmd += ["--exclude-dir", d]
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8")
        hit_files = {Path(line).resolve() for line in r.stdout.splitlines() if line.strip()}
        if not (hit_files - {target}):
            fails.append(f"SLOP=FAIL yagni file={file} line={no} export={name} reason=unreferenced")


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit("SLOP=FAIL usage: check-slop.py <worktree> <base-sha> [--max-complexity N] [--exclude <path> ...]")
    worktree, base = args[0], args[1]
    threshold = 10
    excludes = set()
    i = 2
    while i < len(args):
        if args[i] == "--max-complexity":
            threshold = int(args[i + 1])
            i += 2
        elif args[i] == "--exclude":
            excludes.add(args[i + 1])
            i += 2
        else:
            sys.exit(f"SLOP=FAIL unknown argument {args[i]}")

    files = changed_ts_files(worktree, base, excludes)
    fails, reports = [], []
    for f in files:
        cur_path = Path(worktree) / f
        if not cur_path.exists():
            continue  # deleted file: nothing in the working tree to scan
        base_text = git_show(worktree, base, f)
        cur_text = cur_path.read_text(encoding="utf-8", errors="replace")
        cur_lines = cur_text.splitlines()
        added = added_lines(base_text, cur_lines)
        check_complexity(f, base_text, cur_lines, threshold, fails, reports)
        if f.endswith(".spec.ts") or f.endswith(".test.ts"):
            check_tautological(f, added, cur_lines, fails)
        check_comments(f, added, cur_lines, fails)
        check_yagni(worktree, f, added, fails)

    for line in fails + reports:
        print(line)
    if fails:
        print(f"SLOP=FAIL count={len(fails)}")
        sys.exit(1)
    print(f"SLOP=OK files={len(files)}")


if __name__ == "__main__":
    main()
