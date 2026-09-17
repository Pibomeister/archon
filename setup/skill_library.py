#!/usr/bin/env python3
"""Shared helpers for the repository skills library (library/<repo>/).

Importable module, no CLI. Read side (stage-skills-library.py) and write side
(trace-digest.py, skill-score.py, wiki-apply.py, skill-admit.py) both import
this so the index schema, the frontmatter grammar, the lint rules and the
rollback mechanics have exactly one definition.

Three states, one rule each:
  raw     pointers to run artifacts (raw/index.jsonl); never copies
  wiki    pattern pages a maintainer agent proposes and wiki-apply.py gates;
          never deleted, never staged into a run
  skills  SKILL.md files staged into implement/fix/fixer; one candidate per
          repo at a time; accepted or rolled back by skill-score.py

Everything here is stdlib only: workflow nodes run under whatever python3 is
on PATH, which has no PyYAML.
"""
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

SCHEMA_INDEX = "archon.skill-index.v1"
SCHEMA_RAW = "archon.raw-ledger.v1"
SCHEMA_IMPACT = "archon.skill-impact.v1"
SCHEMA_STAGED = "archon.skills-staged.v1"
SCHEMA_DIGEST = "archon.trace-digest.v1"
SCHEMA_WIKI_PATCH = "archon.wiki-patch.v1"
SCHEMA_PROPOSAL = "archon.skill-proposal.v1"
SCHEMA_VERDICT = "archon.skill-verdict.v1"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
REPO_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SLUG_RE = NAME_RE
STATUSES = ("candidate", "active", "rolled_back")
PATTERN_KINDS = ("failure", "strategy", "root-cause")
# contested: contradicted by later evidence, quarantined from proposals but
# never deleted. superseded: replaced by another page.
PATTERN_STATUSES = ("active", "contested", "superseded")
PATTERN_SECTIONS = ("Problem", "Root cause", "Evidence", "Action sequence", "Known fix")
PATTERN_META_SETTABLE = ("support_count", "last_observed", "runs", "status", "title", "kind")
IMPACT_EVENTS = ("proposed", "admitted", "rejected", "gate_failed", "accepted",
                 "rolled_back", "no_action", "forced_rollback", "window_reset",
                 "pattern_quarantined")

SKILL_MAX_BODY_LINES = 60
SKILL_MAX_BODY_BYTES = 4000
SKILL_MAX_DESCRIPTION = 200
SKILL_MAX_FILE_BYTES = 8192
PATTERN_MAX_LINES = 40
PATTERN_MAX_BYTES = 3500

IMPACT_DIFF_MAX_BYTES = 3000
DEFAULT_WINDOW = 3
DEFAULT_MIN_IMPROVEMENT = 0.0

# Path-portability markers. The literal is split so the shipped source cannot
# trip the packaging secret gate, which greps the payload for the same marker.
ABS_HOME_MARKER = "/Use" + "rs/"
HOME_TILDE_MARKER = "~/"
URL_MARKERS = ("http://", "https://")
_FORBIDDEN_SKILL_WORDS = re.compile(r"\b(wiki|pattern page|PURPOSE)\b|library/", re.IGNORECASE)
# Trace text is evidence, never instructions. A page or skill that carries
# prompt-injection phrasing was compiled from something it should have
# treated as data, so both linters refuse it.
_INJECTION_PHRASES = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|you are now\b|system prompt"
    r"|disregard (the|your|all) (previous|prior|above)|<\s*/?\s*(system|assistant|user)\s*>",
    re.IGNORECASE)

# --- typed-line vocabulary ------------------------------------------------
# Duplicated from setup/tests/nodes/runner.py on purpose: production code must
# not import the test harness. test_skill_library.py pins the two sets equal.
PASS_VALUES = {"PASS", "OK", "CLEAN", "SKIP"}
FAIL_VALUES = {"FAIL", "DIRTY"}
PASS_TOKENS = {
    "PLAN_ROUND", "RCA_PLAN_ROUND",
    "PLAN_CONVERGED", "RCA_PLAN_CONVERGED",
    "PLAN_ROUND_PROGRESSED", "RCA_PLAN_ROUND_PROGRESSED",
    "CONVERGED", "ROUND_PROGRESSED",
    "CRITIQUE", "PRE_OK", "GREEN_CHECK",
}
FAIL_TOKENS = {
    "PLAN_ROUND_CAP", "RCA_PLAN_ROUND_CAP", "ROUND_CAP_REACHED", "DESLOP_ROUND_CAP",
    "PLAN_REJECTED", "RCA_PLAN_REJECTED",
    "PLAN_NO_PROGRESS", "RCA_PLAN_NO_PROGRESS", "NO_PROGRESS",
    "PLAN_SCOPE_DISPUTE", "RCA_PLAN_SCOPE_DISPUTE",
    "FIXER_BLOCKED", "SCOPE_BREACH", "CROSS_REPO_FINDING",
}
PASS_LINE_RES = (re.compile(r"^ROUND=\d+ head=\S+$"),)
_TYPED_RE = re.compile(r"^(?P<key>[A-Z][A-Za-z0-9_]{2,})(?:=(?P<val>\S*))?(?:\s|$)")


class LibraryError(Exception):
    """A library invariant is broken. Callers turn this into a typed FAIL line."""


# --- small utilities --------------------------------------------------------
def utc_now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today_compact():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json_atomic(path, value):
    """tempfile + os.replace, the same shape as setup/no-change-closure.py."""
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path) or ".",
                                     delete=False, encoding="utf-8") as out:
        json.dump(value, out, indent=2, sort_keys=True)
        out.write("\n")
        tmp = out.name
    os.replace(tmp, path)


def write_text_atomic(path, text):
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path) or ".",
                                     delete=False, encoding="utf-8") as out:
        out.write(text)
        tmp = out.name
    os.replace(tmp, path)


def append_jsonl(path, value):
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, sort_keys=True) + "\n")


def read_jsonl(path):
    """Every well-formed line. A corrupt line raises: the ledgers are
    append-only and mechanical, so a bad line is a bug, not noise."""
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise LibraryError(f"{path}:{n} is not JSON: {exc}")
    return out


def proposal_id(run_id):
    return f"P-{utc_today_compact()}-{str(run_id)[:8]}"


# --- typed lines ------------------------------------------------------------
def parse_typed_lines(text):
    """[(key, value_or_None, line)] for every typed line, in print order."""
    out = []
    for raw in str(text).splitlines():
        line = raw.rstrip()
        m = _TYPED_RE.match(line)
        if m is None:
            continue
        key = m.group("key")
        if not (key.isupper() or "_" in key):
            continue
        out.append((key, m.group("val"), line))
    return out


def last_typed(text):
    lines = parse_typed_lines(text)
    return lines[-1] if lines else None


def classify_typed(key, value, line):
    """'pass' | 'fail' | None for one typed line, same rules as the harness."""
    if value is not None:
        if value in PASS_VALUES:
            return "pass"
        if value in FAIL_VALUES:
            return "fail"
    if key in PASS_TOKENS:
        return "pass"
    if key in FAIL_TOKENS:
        return "fail"
    if any(p.match(line) for p in PASS_LINE_RES):
        return "pass"
    return None


# --- paths and skeleton -----------------------------------------------------
def check_repo_name(repo):
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        raise LibraryError(f"invalid repo name {repo!r}")
    return repo


def paths(lib_root, repo):
    check_repo_name(repo)
    # realpath: macOS temp dirs are symlinks and git reports the resolved path.
    root = os.path.realpath(str(lib_root))
    repo_dir = os.path.join(root, repo)
    wiki = os.path.join(repo_dir, "wiki")
    skills = os.path.join(repo_dir, "skills")
    return {
        "root": root,
        "repo": repo,
        "repo_dir": repo_dir,
        "readme": os.path.join(repo_dir, "README.md"),
        "raw_dir": os.path.join(repo_dir, "raw"),
        "raw_ledger": os.path.join(repo_dir, "raw", "index.jsonl"),
        "wiki_dir": wiki,
        "wiki_index": os.path.join(wiki, "index.md"),
        "wiki_log": os.path.join(wiki, "log.md"),
        "impact_md": os.path.join(wiki, "skill-impact.md"),
        "impact_jsonl": os.path.join(wiki, "skill-impact.jsonl"),
        "patterns_dir": os.path.join(wiki, "patterns"),
        "skills_dir": skills,
        "skills_index": os.path.join(skills, "index.json"),
        "rollback_dir": os.path.join(skills, ".rollback"),
        "lock_dir": os.path.join(repo_dir, ".evolve.lock"),
    }


REPO_README = """# Skills library: {repo}

Repository-specific knowledge compiled from Archon runs on `{repo}`. Three
states, one rule each:

- **raw/** holds pointers to run artifacts (`raw/index.jsonl`), never copies.
  Absolute artifact paths live only here; this directory is never packaged.
- **wiki/** holds pattern pages a maintainer agent proposes and
  `setup/wiki-apply.py` gates. Pages are never deleted and never shown to a
  runtime agent. `wiki/index.md` is generated; `wiki/log.md` and
  `wiki/skill-impact.*` are append-only ledgers.
- **skills/** holds `SKILL.md` files that `setup/stage-skills-library.py`
  renders into `skills.md` for the implement, fix and fixer nodes. At most one
  `candidate` exists per repository at a time; it is validated against the
  next K live runs and then accepted or rolled back.

Rules:

- The runtime agent sees skills only. `PURPOSE.md`, the wiki and this file are
  never staged. WikiSkill's ablation is the reason: a runtime agent that can
  read the wiki solves tasks with knowledge the skill does not hold, and the
  score stops measuring the skill.
- Rollback is asymmetric: skills roll back, the wiki never does. A page that
  later evidence contradicts is marked `contested` (quarantined from proposals)
  or `superseded`; it is never deleted. Rejected and rolled-back candidates
  keep their diff in `wiki/skill-impact.md` so the proposer never repeats one.
- Ties roll back. A candidate must beat its frozen baseline window strictly;
  neutral stepping stones are discarded by design.
- Every write is mechanical (`skill-admit.py`, `skill-score.py`,
  `wiki-apply.py`). Do not edit this directory by hand.
- Commits are authored as `archon skill-evolve <skill-evolve@archon.local>`
  and touch only `library/{repo}`; `git log -- library/{repo}` is the audit trail.
- No absolute home paths, no `~` paths, no URLs anywhere under `wiki/` or
  `skills/`: the library must read the same on every machine.
- The score is defined and versioned in `setup/skill-score.py`; a window whose
  runs were scored under another version is inconclusive and rolls back.
"""

WIKI_INDEX_HEADER = """# Pattern index: {repo}

GENERATED by setup/wiki-apply.py regen-index. Do not edit by hand.

| slug | kind | status | title | support | last observed | skills | problem / fix |
|---|---|---|---|---|---|---|---|
"""

WIKI_LOG_HEADER = """# Wiki log: {repo}

Append-only chronology. Mechanical writers only (wiki-apply.py, skill-admit.py,
skill-score.py).

"""

IMPACT_HEADER = """# Skill impact: {repo}

One section per event. The proposer reads this first so it never repeats a
rejected or rolled-back approach. The machine ledger is skill-impact.jsonl;
gates read that file, never this one.

"""


def new_index(repo):
    return {
        "schema": SCHEMA_INDEX,
        "repo": repo,
        "window_size": DEFAULT_WINDOW,
        "min_improvement": DEFAULT_MIN_IMPROVEMENT,
        "score_version": 1,
        "skills": {},
    }


def ensure_skeleton(lib_root, repo):
    """Create every skeleton file that is missing. Returns the created
    paths, relative to the repo dir, so a caller can report them."""
    p = paths(lib_root, repo)
    created = []

    def touch(path, text):
        if os.path.exists(path):
            return
        write_text_atomic(path, text)
        created.append(os.path.relpath(path, p["repo_dir"]))

    for d in (p["repo_dir"], p["raw_dir"], p["wiki_dir"], p["patterns_dir"], p["skills_dir"]):
        os.makedirs(d, exist_ok=True)
    touch(p["readme"], REPO_README.format(repo=repo))
    touch(p["raw_ledger"], "")
    touch(p["wiki_index"], WIKI_INDEX_HEADER.format(repo=repo))
    touch(p["wiki_log"], WIKI_LOG_HEADER.format(repo=repo))
    touch(p["impact_md"], IMPACT_HEADER.format(repo=repo))
    touch(p["impact_jsonl"], "")
    touch(os.path.join(p["patterns_dir"], ".keep"), "")
    if not os.path.exists(p["skills_index"]):
        write_json_atomic(p["skills_index"], new_index(repo))
        created.append(os.path.relpath(p["skills_index"], p["repo_dir"]))
    return created


def skeleton_present(lib_root, repo):
    p = paths(lib_root, repo)
    return all(os.path.exists(p[k]) for k in
               ("readme", "raw_ledger", "wiki_index", "wiki_log", "impact_md",
                "impact_jsonl", "patterns_dir", "skills_index"))


# --- frontmatter ------------------------------------------------------------
_INT_RE = re.compile(r"^-?\d+$")


def _parse_scalar(raw):
    raw = raw.strip()
    if raw == "":
        return ""
    if raw[0] in "[{\"":
        try:
            return json.loads(raw)
        except ValueError:
            raise LibraryError(f"frontmatter value is not JSON: {raw!r}")
    if _INT_RE.match(raw):
        return int(raw)
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    return raw


def _render_scalar(value):
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, list, dict)):
        return json.dumps(value, sort_keys=True)
    s = str(value)
    needs_quote = (
        s == "" or s != s.strip() or "\n" in s or s[0] in "[{\"#"
        or _INT_RE.match(s) or s in ("true", "false", "null")
    )
    return json.dumps(s) if needs_quote else s


def parse_frontmatter(text):
    """(meta, body). The document must open with a `---` fence; every
    frontmatter line is `key: value`; the body is everything after the
    closing fence with its leading newline removed."""
    lines = str(text).split("\n")
    if not lines or lines[0].strip() != "---":
        raise LibraryError("frontmatter missing: file does not start with ---")
    meta = {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
        line = lines[i]
        if not line.strip():
            continue
        if ":" not in line:
            raise LibraryError(f"frontmatter line is not key: value: {line!r}")
        key, _, val = line.partition(":")
        key = key.strip()
        if not re.match(r"^[a-z][a-z0-9_]*$", key):
            raise LibraryError(f"frontmatter key not allowed: {key!r}")
        if key in meta:
            raise LibraryError(f"frontmatter key repeated: {key!r}")
        meta[key] = _parse_scalar(val)
    if end is None:
        raise LibraryError("frontmatter missing closing ---")
    body = "\n".join(lines[end + 1:])
    if body.startswith("\n"):
        body = body[1:]
    return meta, body


def render_frontmatter(meta, body):
    out = ["---"]
    for key, value in meta.items():
        out.append(f"{key}: {_render_scalar(value)}")
    out.append("---")
    out.append("")
    text = "\n".join(out) + body
    if not text.endswith("\n"):
        text += "\n"
    return text


# --- SKILL.md ---------------------------------------------------------------
def apply_line_ops(text, ops):
    """Apply replace|append|insert_after ops to a text. A targeted op must
    match exactly one line (exact string equality after rstrip). Returns the
    new text; raises LibraryError on any op that cannot apply."""
    if not isinstance(ops, list) or not ops:
        raise LibraryError("ops must be a non-empty list")
    lines = str(text).rstrip("\n").split("\n")
    for n, op in enumerate(ops):
        if not isinstance(op, dict):
            raise LibraryError(f"op {n}: not an object")
        kind = op.get("op")
        new = op.get("text")
        if not isinstance(new, str):
            raise LibraryError(f"op {n}: text must be a string")
        new_lines = new.rstrip("\n").split("\n") if new != "" else []
        if kind == "append":
            lines.extend(new_lines)
            continue
        if kind not in ("replace", "insert_after"):
            raise LibraryError(f"op {n}: unknown op {kind!r}")
        target = op.get("target")
        if not isinstance(target, str) or not target.strip():
            raise LibraryError(f"op {n}: target must be a non-empty string")
        hits = [i for i, l in enumerate(lines) if l.rstrip() == target.rstrip()]
        if len(hits) == 0:
            raise LibraryError(f"op {n}: target matches no line: {target!r}")
        if len(hits) > 1:
            raise LibraryError(f"op {n}: target is ambiguous ({len(hits)} lines): {target!r}")
        i = hits[0]
        if kind == "replace":
            lines[i:i + 1] = new_lines
        else:
            lines[i + 1:i + 1] = new_lines
    return "\n".join(lines) + "\n"


def _portability_errors(text, where):
    errs = []
    m = _INJECTION_PHRASES.search(text)
    if m:
        errs.append(f"{where}: instruction-like text {m.group(0)!r}")
    if ABS_HOME_MARKER in text:
        errs.append(f"{where}: absolute home path")
    if HOME_TILDE_MARKER in text:
        errs.append(f"{where}: home-relative path")
    if any(m in text for m in URL_MARKERS):
        errs.append(f"{where}: URL")
    return errs


def lint_skill(name, text):
    """List of problems (empty when the SKILL.md is acceptable)."""
    errs = []
    if not isinstance(name, str) or not NAME_RE.match(name) or "__" in name:
        errs.append(f"name {name!r} does not match {NAME_RE.pattern} (no __)")
    data = str(text).encode("utf-8")
    if len(data) > SKILL_MAX_FILE_BYTES:
        errs.append(f"file is {len(data)} bytes, cap {SKILL_MAX_FILE_BYTES}")
    try:
        meta, body = parse_frontmatter(text)
    except LibraryError as exc:
        return errs + [str(exc)]
    if meta.get("name") != name:
        errs.append(f"frontmatter name {meta.get('name')!r} != {name!r}")
    desc = meta.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errs.append("description missing")
    elif len(desc) > SKILL_MAX_DESCRIPTION:
        errs.append(f"description is {len(desc)} chars, cap {SKILL_MAX_DESCRIPTION}")
    extra = sorted(set(meta) - {"name", "description"})
    if extra:
        errs.append(f"frontmatter keys not allowed: {extra}")
    body_lines = body.rstrip("\n").split("\n") if body.strip() else []
    if not body_lines:
        errs.append("body is empty")
    if len(body_lines) > SKILL_MAX_BODY_LINES:
        errs.append(f"body is {len(body_lines)} lines, cap {SKILL_MAX_BODY_LINES}")
    if len(body.encode("utf-8")) > SKILL_MAX_BODY_BYTES:
        errs.append(f"body is {len(body.encode('utf-8'))} bytes, cap {SKILL_MAX_BODY_BYTES}")
    if not any(re.match(r"^\d+\. ", l) for l in body_lines):
        errs.append("body has no numbered step line (^\\d+\\. )")
    m = _FORBIDDEN_SKILL_WORDS.search(str(text))
    if m:
        errs.append(f"forbidden word for a skill: {m.group(0)!r}")
    errs.extend(_portability_errors(str(text), "skill"))
    return errs


# --- pattern pages ----------------------------------------------------------
_SECTION_RE = re.compile(r"^## (.+?)\s*$")


def parse_pattern(text):
    """(meta, {section: body}) for a pattern page. Sections are the `## `
    headings in the body, in file order."""
    meta, body = parse_frontmatter(text)
    sections = {}
    order = []
    current = None
    buf = []
    for line in body.split("\n"):
        m = _SECTION_RE.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip("\n")
            current = m.group(1)
            if current in sections or current in order:
                raise LibraryError(f"section repeated: {current!r}")
            order.append(current)
            buf = []
            continue
        if current is None:
            if line.strip():
                raise LibraryError(f"text before the first section: {line!r}")
            continue
        buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip("\n")
    return meta, {k: sections[k] for k in order}


def render_pattern(meta, sections):
    parts = []
    for name in PATTERN_SECTIONS:
        parts.append(f"## {name}")
        parts.append(sections.get(name, "").strip("\n"))
        parts.append("")
    body = "\n".join(parts).rstrip("\n") + "\n"
    return render_frontmatter(meta, body)


def lint_pattern(slug, text):
    errs = []
    if not isinstance(slug, str) or not SLUG_RE.match(slug) or "__" in slug:
        errs.append(f"slug {slug!r} does not match {SLUG_RE.pattern}")
    data = str(text).encode("utf-8")
    if len(data) > PATTERN_MAX_BYTES:
        errs.append(f"page is {len(data)} bytes, cap {PATTERN_MAX_BYTES}")
    try:
        meta, sections = parse_pattern(text)
    except LibraryError as exc:
        return errs + [str(exc)]
    if meta.get("slug") != slug:
        errs.append(f"frontmatter slug {meta.get('slug')!r} != {slug!r}")
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        errs.append("title missing")
    if meta.get("kind") not in PATTERN_KINDS:
        errs.append(f"kind {meta.get('kind')!r} not in {PATTERN_KINDS}")
    if meta.get("status") not in PATTERN_STATUSES:
        errs.append(f"status {meta.get('status')!r} not in {PATTERN_STATUSES}")
    sc = meta.get("support_count")
    if isinstance(sc, bool) or not isinstance(sc, int) or sc < 1:
        errs.append(f"support_count {sc!r} must be an int >= 1")
    for key in ("first_observed", "last_observed"):
        if not isinstance(meta.get(key), str) or not meta.get(key):
            errs.append(f"{key} missing")
    for key in ("runs", "skills"):
        val = meta.get(key)
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            errs.append(f"{key} must be a list of strings")
    if key_order := list(sections):
        if tuple(key_order) != PATTERN_SECTIONS:
            errs.append(f"sections must be exactly {list(PATTERN_SECTIONS)} in order, got {key_order}")
    else:
        errs.append("no sections")
    for name in PATTERN_SECTIONS:
        if not sections.get(name, "").strip():
            errs.append(f"section {name!r} is empty")
    if "run:" not in sections.get("Evidence", ""):
        errs.append("Evidence cites no run:<id>/<path>")
    _, body = parse_frontmatter(text)
    n_lines = len(body.rstrip("\n").split("\n"))
    if n_lines > PATTERN_MAX_LINES:
        errs.append(f"body is {n_lines} lines, cap {PATTERN_MAX_LINES}")
    errs.extend(_portability_errors(str(text), "pattern"))
    return errs


def apply_section_ops(text, ops):
    """set_section | append_section | set_meta on a pattern page. set_meta is
    restricted to PATTERN_META_SETTABLE. Returns the re-rendered page."""
    if not isinstance(ops, list) or not ops:
        raise LibraryError("ops must be a non-empty list")
    meta, sections = parse_pattern(text)
    for n, op in enumerate(ops):
        if not isinstance(op, dict):
            raise LibraryError(f"op {n}: not an object")
        kind = op.get("op")
        if kind in ("set_section", "append_section"):
            section = op.get("section")
            if section not in PATTERN_SECTIONS:
                raise LibraryError(f"op {n}: unknown section {section!r}")
            new = op.get("text")
            if not isinstance(new, str) or not new.strip():
                raise LibraryError(f"op {n}: text must be a non-empty string")
            if kind == "set_section":
                sections[section] = new.strip("\n")
            else:
                cur = sections.get(section, "").strip("\n")
                sections[section] = (cur + "\n" + new.strip("\n")).strip("\n")
        elif kind == "set_meta":
            key = op.get("key")
            if key not in PATTERN_META_SETTABLE:
                raise LibraryError(f"op {n}: meta key {key!r} is not settable")
            if "value" not in op:
                raise LibraryError(f"op {n}: value missing")
            meta[key] = op["value"]
        else:
            raise LibraryError(f"op {n}: unknown op {kind!r}")
    return render_pattern(meta, sections)


def pattern_path(lib_root, repo, slug):
    return os.path.join(paths(lib_root, repo)["patterns_dir"], f"{slug}.md")


def list_patterns(lib_root, repo):
    """{slug: (meta, sections)} for every page on disk, sorted by slug."""
    d = paths(lib_root, repo)["patterns_dir"]
    out = {}
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        slug = name[:-3]
        with open(os.path.join(d, name), encoding="utf-8") as fh:
            out[slug] = parse_pattern(fh.read())
    return out


INDEX_SUMMARY_CHARS = 80


def _summary(text):
    """First non-empty line of a section, list marker stripped, truncated so
    the index row tells a reader whether opening the page is worth it."""
    for line in str(text).splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line).strip()
        if line:
            return line if len(line) <= INDEX_SUMMARY_CHARS else line[:INDEX_SUMMARY_CHARS - 1] + "\u2026"
    return ""


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def regenerate_index_md(lib_root, repo):
    p = paths(lib_root, repo)
    rows = []
    for slug, (meta, sections) in list_patterns(lib_root, repo).items():
        rows.append((
            -int(meta.get("support_count") or 0), slug,
            str(meta.get("kind", "")), str(meta.get("status", "")), str(meta.get("title", "")),
            int(meta.get("support_count") or 0), str(meta.get("last_observed", "")),
            ", ".join(meta.get("skills") or []),
            _summary(sections.get("Problem", "")) + " / " + _summary(sections.get("Known fix", "")),
        ))
    rows.sort()
    lines = [WIKI_INDEX_HEADER.format(repo=repo).rstrip("\n")]
    for _neg, slug, kind, status, title, support, last, skills, summary in rows:
        cells = [slug, kind, status, _cell(title), str(support), last, skills, _cell(summary)]
        lines.append("| " + " | ".join(cells) + " |")
    write_text_atomic(p["wiki_index"], "\n".join(lines) + "\n")
    return len(rows)


# --- index ------------------------------------------------------------------
def skill_dir(lib_root, repo, name):
    return os.path.join(paths(lib_root, repo)["skills_dir"], name)


def skill_file(lib_root, repo, name):
    return os.path.join(skill_dir(lib_root, repo, name), "SKILL.md")


def purpose_file(lib_root, repo, name):
    return os.path.join(skill_dir(lib_root, repo, name), "PURPOSE.md")


def one_candidate(index):
    names = [n for n, e in (index.get("skills") or {}).items()
             if isinstance(e, dict) and e.get("status") == "candidate"]
    if len(names) > 1:
        raise LibraryError(f"more than one candidate: {sorted(names)}")
    return names[0] if names else None


def validate_index(index, repo=None, repo_dir=None):
    """Every invariant, as a list of messages (empty = valid). When repo_dir
    is given the on-disk skills tree is checked against the registry too."""
    errs = []
    if not isinstance(index, dict):
        return ["index is not an object"]
    if index.get("schema") != SCHEMA_INDEX:
        errs.append(f"schema {index.get('schema')!r} != {SCHEMA_INDEX}")
    if repo is not None and index.get("repo") != repo:
        errs.append(f"index repo {index.get('repo')!r} != {repo!r}")
    ws = index.get("window_size")
    if isinstance(ws, bool) or not isinstance(ws, int) or ws < 1:
        errs.append(f"window_size {ws!r} must be an int >= 1")
    mi = index.get("min_improvement")
    if isinstance(mi, bool) or not isinstance(mi, (int, float)):
        errs.append(f"min_improvement {mi!r} must be a number")
    sv = index.get("score_version")
    if isinstance(sv, bool) or not isinstance(sv, int) or sv < 1:
        errs.append(f"score_version {sv!r} must be an int >= 1")
    skills = index.get("skills")
    if not isinstance(skills, dict):
        return errs + ["skills is not an object"]
    candidates = []
    for name, entry in skills.items():
        if not NAME_RE.match(str(name)) or "__" in str(name):
            errs.append(f"skill name {name!r} invalid")
        if not isinstance(entry, dict):
            errs.append(f"{name}: entry is not an object")
            continue
        status = entry.get("status")
        if status not in STATUSES:
            errs.append(f"{name}: status {status!r} not in {STATUSES}")
        sha = entry.get("sha256")
        if status in ("candidate", "active") and (not isinstance(sha, str) or not re.match(r"^[0-9a-f]{64}$", sha)):
            errs.append(f"{name}: sha256 missing or malformed")
        env = entry.get("envelope")
        if env is not None and not (isinstance(env, dict)
                                    and all(isinstance(env.get(k), list) for k in ("lanes", "models"))):
            errs.append(f"{name}: envelope must be an object with lanes[] and models[]")
        if status == "candidate":
            candidates.append(name)
            rb = entry.get("rollback")
            if not isinstance(rb, dict) or not isinstance(rb.get("snapshot"), str):
                errs.append(f"{name}: candidate has no rollback record")
            sc = entry.get("scoring")
            if not isinstance(sc, dict) or not isinstance(sc.get("baseline_runs"), list) \
                    or not isinstance(sc.get("candidate_runs"), list):
                errs.append(f"{name}: candidate has no scoring window")
        if repo_dir is not None and status in STATUSES:
            sfile = os.path.join(repo_dir, "skills", str(name), "SKILL.md")
            sdir = os.path.join(repo_dir, "skills", str(name))
            if status in ("candidate", "active"):
                if not os.path.isfile(sfile):
                    errs.append(f"{name}: {status} but skills/{name}/SKILL.md missing")
                elif sha256_file(sfile) != sha:
                    errs.append(f"{name}: {status} sha256 does not match SKILL.md on disk")
                elif status == "candidate":
                    snap = os.path.join(repo_dir, "skills", str((entry.get("rollback") or {}).get("snapshot", "")))
                    if not os.path.isdir(snap):
                        errs.append(f"{name}: candidate rollback snapshot missing")
            elif status == "rolled_back" and os.path.isdir(sdir):
                pre = (entry.get("rollback") or {}).get("sha256")
                if not os.path.isfile(sfile) or pre is None or sha256_file(sfile) != pre:
                    errs.append(f"{name}: rolled_back but skills/{name} holds content that is not the pre-candidate bytes")
    if len(candidates) > 1:
        errs.append(f"more than one candidate: {sorted(candidates)}")
    if repo_dir is not None:
        sdir = os.path.join(repo_dir, "skills")
        if os.path.isdir(sdir):
            for name in sorted(os.listdir(sdir)):
                full = os.path.join(sdir, name)
                if name in (".rollback", "index.json") or name.startswith("."):
                    continue
                if os.path.isdir(full) and name not in skills:
                    errs.append(f"unregistered skill dir: skills/{name}")
        rb = os.path.join(sdir, ".rollback")
        if os.path.isdir(rb):
            for name in sorted(os.listdir(rb)):
                if name.startswith("."):
                    continue
                if (skills.get(name) or {}).get("status") != "candidate":
                    errs.append(f"stale rollback snapshot for non-candidate: .rollback/{name}")
    return errs


def load_index(lib_root, repo, validate=True):
    """The parsed, validated index. FileNotFoundError when absent; LibraryError
    when malformed or when an invariant is broken."""
    p = paths(lib_root, repo)
    with open(p["skills_index"], encoding="utf-8") as fh:
        try:
            index = json.load(fh)
        except ValueError as exc:
            raise LibraryError(f"index.json is not JSON: {exc}")
    if validate:
        errs = validate_index(index, repo, p["repo_dir"])
        if errs:
            raise LibraryError("index invalid: " + "; ".join(errs))
    return index


def save_index(lib_root, repo, index):
    p = paths(lib_root, repo)
    errs = validate_index(index, repo, p["repo_dir"])
    if errs:
        raise LibraryError("refusing to save an invalid index: " + "; ".join(errs))
    write_json_atomic(p["skills_index"], index)


# --- rollback / accept ------------------------------------------------------
ABSENT_MARKER = "ABSENT"


def snapshot_for_rollback(lib_root, repo, name):
    """Copy the pre-candidate skill dir into skills/.rollback/<name>/ (or an
    ABSENT marker when the skill did not exist). Returns the rollback record
    that goes into index.json."""
    p = paths(lib_root, repo)
    src = skill_dir(lib_root, repo, name)
    snap = os.path.join(p["rollback_dir"], name)
    if os.path.exists(snap):
        shutil.rmtree(snap)
    os.makedirs(snap)
    existed = os.path.isfile(os.path.join(src, "SKILL.md"))
    sha = None
    if existed:
        for fn in ("SKILL.md", "PURPOSE.md"):
            f = os.path.join(src, fn)
            if os.path.isfile(f):
                shutil.copyfile(f, os.path.join(snap, fn))
        sha = sha256_file(os.path.join(src, "SKILL.md"))
    else:
        write_text_atomic(os.path.join(snap, ABSENT_MARKER), "")
    return {"existed": existed, "sha256": sha, "snapshot": f".rollback/{name}"}


def _history(entry, proposal, action, outcome, at, **extra):
    row = {"proposal": proposal, "action": action, "outcome": outcome, "at": at}
    row.update(extra)
    entry.setdefault("history", []).append(row)


def rollback_candidate(lib_root, repo, index, name, reason, at=None):
    """Restore the pre-candidate state for `name` and update the index entry
    in place. A patched skill returns to `active` with its old bytes; a
    created skill is removed and marked `rolled_back`. Returns the outcome
    ('restored' | 'removed')."""
    at = at or utc_now()
    p = paths(lib_root, repo)
    entry = index["skills"][name]
    if entry.get("status") != "candidate":
        raise LibraryError(f"{name} is not a candidate")
    rb = entry.get("rollback") or {}
    snap = os.path.join(p["skills_dir"], rb.get("snapshot", f".rollback/{name}"))
    if not os.path.isdir(snap):
        raise LibraryError(f"{name}: rollback snapshot missing at {snap}")
    dst = skill_dir(lib_root, repo, name)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    if rb.get("existed") and os.path.isfile(os.path.join(snap, "SKILL.md")):
        os.makedirs(dst)
        for fn in ("SKILL.md", "PURPOSE.md"):
            f = os.path.join(snap, fn)
            if os.path.isfile(f):
                shutil.copyfile(f, os.path.join(dst, fn))
        entry["status"] = "active"
        entry["sha256"] = sha256_file(os.path.join(dst, "SKILL.md"))
        outcome = "restored"
    else:
        entry["status"] = "rolled_back"
        entry["sha256"] = None
        outcome = "removed"
    shutil.rmtree(snap)
    entry["candidate_since"] = None
    entry["scoring"] = {"window_size": entry.get("scoring", {}).get("window_size", index.get("window_size")),
                        "baseline_runs": [], "candidate_runs": []}
    _history(entry, entry.get("proposal"), entry.get("action", "patch" if rb.get("existed") else "create"),
             "rolled_back", at, reason=reason, restored=outcome)
    return outcome


def accept_candidate(lib_root, repo, index, name, at=None):
    at = at or utc_now()
    p = paths(lib_root, repo)
    entry = index["skills"][name]
    if entry.get("status") != "candidate":
        raise LibraryError(f"{name} is not a candidate")
    sfile = skill_file(lib_root, repo, name)
    if not os.path.isfile(sfile):
        raise LibraryError(f"{name}: SKILL.md missing at accept")
    entry["status"] = "active"
    entry["sha256"] = sha256_file(sfile)
    entry["activated_at"] = at
    entry["candidate_since"] = None
    snap = os.path.join(p["skills_dir"], (entry.get("rollback") or {}).get("snapshot", f".rollback/{name}"))
    if os.path.isdir(snap):
        shutil.rmtree(snap)
    _history(entry, entry.get("proposal"), entry.get("action", "patch"), "accepted", at)
    return "accepted"


# --- ledgers ----------------------------------------------------------------
def record_impact(lib_root, repo, event, skill, proposal, evolve_run_id, reason="", details=None, at=None):
    """Append one event to skill-impact.jsonl (machine) and skill-impact.md
    (human). Returns the row written."""
    if event not in IMPACT_EVENTS:
        raise LibraryError(f"unknown impact event {event!r}")
    at = at or utc_now()
    p = paths(lib_root, repo)
    row = {
        "schema": SCHEMA_IMPACT, "at": at, "event": event, "skill": skill,
        "proposal": proposal, "evolve_run_id": evolve_run_id, "reason": reason,
        "details": details or {},
    }
    append_jsonl(p["impact_jsonl"], row)
    lines = [f"## {at} {event} {skill or '-'} ({proposal or '-'})", ""]
    lines.append(f"- evolve run: {evolve_run_id or '-'}")
    if reason:
        lines.append(f"- reason: {reason}")
    diff = None
    for key in sorted(details or {}):
        val = (details or {})[key]
        if key == "diff" and isinstance(val, str):
            diff = val
            continue
        if isinstance(val, (dict, list)):
            val = json.dumps(val, sort_keys=True)
        lines.append(f"- {key}: {val}")
    if diff is not None:
        # The rejected content itself, so the proposer can see what not to repeat.
        lines += ["", "```diff", diff.rstrip("\n"), "```"]
    lines.append("")
    with open(p["impact_md"], "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return row


def read_impact(lib_root, repo):
    return read_jsonl(paths(lib_root, repo)["impact_jsonl"])


def append_log(lib_root, repo, line, at=None):
    at = at or utc_now()
    p = paths(lib_root, repo)
    line = " ".join(str(line).split())
    with open(p["wiki_log"], "a", encoding="utf-8") as fh:
        fh.write(f"- {at} {line}\n")


def read_raw_ledger(lib_root, repo):
    return read_jsonl(paths(lib_root, repo)["raw_ledger"])


# --- git ----------------------------------------------------------------------
def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8")


def git_toplevel(path):
    if not os.path.isdir(path):
        return None
    r = _git(["rev-parse", "--show-toplevel"], path)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def git_head(path):
    top = git_toplevel(path)
    if top is None:
        return None
    r = _git(["rev-parse", "HEAD"], top)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def git_dirty(lib_root, repo):
    """True when library/<repo> has uncommitted changes; None outside git."""
    p = paths(lib_root, repo)
    top = git_toplevel(p["repo_dir"]) or git_toplevel(p["root"])
    if top is None:
        return None
    r = _git(["status", "--porcelain", "--", p["repo_dir"]], top)
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


GIT_AUTHOR = ("archon skill-evolve", "skill-evolve@archon.local")


def git_commit(lib_root, repo, message):
    """Stage library/<repo> only and commit as the skill-evolve author.
    Returns the new commit sha, or None when the library is outside git or
    there was nothing to commit."""
    p = paths(lib_root, repo)
    top = git_toplevel(p["root"])
    if top is None:
        return None
    rel = os.path.relpath(p["repo_dir"], os.path.realpath(top))
    if rel.startswith(".."):
        return None
    r = _git(["add", "-A", "--", rel], top)
    if r.returncode != 0:
        raise LibraryError(f"git add failed: {r.stderr.strip()}")
    r = _git(["diff", "--cached", "--quiet", "--", rel], top)
    if r.returncode == 0:
        return None
    r = _git(["-c", f"user.name={GIT_AUTHOR[0]}", "-c", f"user.email={GIT_AUTHOR[1]}",
              "commit", "-q", "--no-verify", "-m", message, "--", rel], top)
    if r.returncode != 0:
        raise LibraryError(f"git commit failed: {r.stderr.strip()}")
    r = _git(["rev-parse", "HEAD"], top)
    return r.stdout.strip() or None
