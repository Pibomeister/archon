#!/usr/bin/env python3
"""The only writer of library/<repo>/wiki.

The wiki-maintain agent proposes; this gate applies. Every page is built in
memory, linted, and only then written, so a run either lands whole or not at
all. Pages are never deleted: a wrong page is quarantined (status contested)
and stays as evidence.

  wiki-apply.py apply <repo> <wiki-patch.json> --lib <dir> --digest <trace-digest.json>
                      [--out <json>] [--dry-run]
  wiki-apply.py quarantine <repo> <slug> --lib <dir> --reason "<text>" [--evolve-run <id>]
  wiki-apply.py regen-index <repo> --lib <dir>
  wiki-apply.py lint <repo> --lib <dir>

Input schema archon.wiki-patch.v1:
  {"schema": "archon.wiki-patch.v1",
   "pages": [{"op": "create", "slug": ..., "meta": {"title": ..., "kind": ...},
              "sections": {"Problem": ..., "Root cause": ..., "Evidence": ...,
                           "Action sequence": ..., "Known fix": ...}},
             {"op": "patch", "slug": ..., "ops": [
                 {"op": "append_section"|"set_section", "section": ..., "text": ...},
                 {"op": "set_meta", "key": ..., "value": ...}]}],
   "log": "<= 200 chars", "notes": "ignored"}

Gate rules (all mechanical):
  - at most 3 creates and 6 patches per run; a slug appears at most once
  - create on an existing slug, patch on an unknown slug, unknown op: FAIL
  - a required section is never emptied; `skills` and `runs` meta are not the
    maintainer's to set (runs is unioned with the digest run id automatically)
  - support_count may grow by at most 1 per run, only when the run id is new
    to the page; status and kind must be in the library enums
  - every `run:<id>` cited in Evidence must be in raw/index.jsonl or be the
    run being ingested, and at least one citation is required
  - no absolute or home-relative paths, no URLs, no instruction-like text
    (trace text is data, never instructions); size caps from skill_library
  - library/<repo> must be clean in git before apply (outside git: allowed)

Typed lines (the last one is the discriminator):
  WIKI_GATE=PASS created=n patched=m index_regenerated=yes|no log_appended=yes|no [dry_run=yes]
  WIKI_GATE=FAIL <reason>                                    exit 1
  LIBRARY_COMMIT=OK sha=<12> | LIBRARY_COMMIT=SKIP no git | LIBRARY_COMMIT=SKIP nothing to commit
  WIKI_QUARANTINE=OK slug=.. status=contested | WIKI_QUARANTINE=FAIL <reason>
  WIKI_INDEX=OK rows=n
  WIKI_LINT=OK pages=n | WIKI_LINT=FAIL slug=<s> <first error>
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_library as sl  # noqa: E402

MAX_CREATES = 3
MAX_PATCHES = 6
MAX_LOG_CHARS = 200
RUN_CITE_RE = re.compile(r"run:([A-Za-z0-9._-]+)")
GATE_ERRORS = (sl.LibraryError, ValueError, TypeError, KeyError, OSError)


class GateFail(Exception):
    pass


def _read_page(lib_root, repo, slug):
    path = sl.pattern_path(lib_root, repo, slug)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _known_runs(lib_root, repo, run_id):
    known = {run_id}
    for row in sl.read_raw_ledger(lib_root, repo):
        rid = row.get("run_id") if isinstance(row, dict) else None
        if isinstance(rid, str) and rid:
            known.add(rid)
    return known


def _check_evidence(slug, text, known):
    _, sections = sl.parse_pattern(text)
    cited = RUN_CITE_RE.findall(sections.get("Evidence", ""))
    if not cited:
        raise GateFail(f"page {slug}: Evidence cites no run:<id>")
    unknown = sorted(set(cited) - known)
    if unknown:
        raise GateFail(f"page {slug}: Evidence cites unknown run {unknown[0]!r}")


def _slug_ok(slug):
    return isinstance(slug, str) and sl.SLUG_RE.match(slug) and "__" not in slug


def _build_create(page, run_id, now):
    slug = page.get("slug")
    if not _slug_ok(slug):
        raise GateFail(f"create: invalid slug {slug!r}")
    meta_in = page.get("meta")
    if not isinstance(meta_in, dict):
        raise GateFail(f"page {slug}: meta must be an object")
    extra = sorted(set(meta_in) - {"title", "kind"})
    if extra:
        raise GateFail(f"page {slug}: meta key {extra[0]!r} is not the maintainer's to set")
    title = meta_in.get("title")
    if not isinstance(title, str) or not title.strip():
        raise GateFail(f"page {slug}: title missing")
    kind = meta_in.get("kind")
    if kind not in sl.PATTERN_KINDS:
        raise GateFail(f"page {slug}: kind {kind!r} not in {list(sl.PATTERN_KINDS)}")
    sections_in = page.get("sections")
    if not isinstance(sections_in, dict):
        raise GateFail(f"page {slug}: sections must be an object")
    unknown = sorted(set(sections_in) - set(sl.PATTERN_SECTIONS))
    if unknown:
        raise GateFail(f"page {slug}: unknown section {unknown[0]!r}")
    sections = {}
    for name in sl.PATTERN_SECTIONS:
        val = sections_in.get(name)
        if not isinstance(val, str) or not val.strip():
            raise GateFail(f"page {slug}: section {name!r} is empty")
        sections[name] = val.strip("\n")
    meta = {
        "slug": slug, "title": title.strip(), "kind": kind, "support_count": 1,
        "first_observed": now, "last_observed": now, "runs": [run_id],
        "skills": [], "status": "active",
    }
    return sl.render_pattern(meta, sections)


def _build_patch(page, current, run_id, now):
    slug = page.get("slug")
    ops = page.get("ops")
    if not isinstance(ops, list) or not ops:
        raise GateFail(f"page {slug}: ops must be a non-empty list")
    old_meta, _ = sl.parse_pattern(current)
    old_runs = list(old_meta.get("runs") or [])
    run_is_new = run_id not in old_runs
    old_support = int(old_meta.get("support_count") or 0)
    saw_last_observed = False
    for n, op in enumerate(ops):
        if not isinstance(op, dict):
            raise GateFail(f"page {slug}: op {n} is not an object")
        kind = op.get("op")
        if kind == "set_meta":
            key = op.get("key")
            value = op.get("value")
            if key in ("skills", "runs"):
                raise GateFail(f"page {slug}: op {n}: meta key {key!r} is not the maintainer's to set")
            if key not in sl.PATTERN_META_SETTABLE:
                raise GateFail(f"page {slug}: op {n}: meta key {key!r} is not settable")
            if key == "status" and value not in sl.PATTERN_STATUSES:
                raise GateFail(f"page {slug}: op {n}: status {value!r} not in {list(sl.PATTERN_STATUSES)}")
            if key == "kind" and value not in sl.PATTERN_KINDS:
                raise GateFail(f"page {slug}: op {n}: kind {value!r} not in {list(sl.PATTERN_KINDS)}")
            if key == "support_count":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise GateFail(f"page {slug}: op {n}: support_count must be an int")
                ceiling = old_support + (1 if run_is_new else 0)
                if value < old_support or value > ceiling:
                    raise GateFail(f"page {slug}: op {n}: support_count {value} outside "
                                   f"[{old_support}, {ceiling}] for run {run_id}")
            if key in ("title", "last_observed") and (not isinstance(value, str) or not value.strip()):
                raise GateFail(f"page {slug}: op {n}: {key} must be a non-empty string")
            if key == "last_observed":
                saw_last_observed = True
        elif kind not in ("set_section", "append_section"):
            raise GateFail(f"page {slug}: op {n}: unknown op {kind!r}")
    try:
        text = sl.apply_section_ops(current, ops)
    except sl.LibraryError as exc:
        raise GateFail(f"page {slug}: {exc}")
    meta, sections = sl.parse_pattern(text)
    if run_is_new:
        meta["runs"] = old_runs + [run_id]
        if not saw_last_observed:
            meta["last_observed"] = now
    return sl.render_pattern(meta, sections)


def _load_patch(path):
    try:
        doc = sl.read_json(path)
    except (OSError, ValueError) as exc:
        raise GateFail(f"wiki-patch is not JSON: {exc}")
    if not isinstance(doc, dict) or doc.get("schema") != sl.SCHEMA_WIKI_PATCH:
        raise GateFail(f"wiki-patch schema must be {sl.SCHEMA_WIKI_PATCH}")
    pages = doc.get("pages")
    if not isinstance(pages, list):
        raise GateFail("pages must be a list")
    log = doc.get("log")
    if not isinstance(log, str) or not log.strip():
        raise GateFail("log must be a non-empty string")
    log = " ".join(log.split())
    if len(log) > MAX_LOG_CHARS:
        raise GateFail(f"log is {len(log)} chars, cap {MAX_LOG_CHARS}")
    if sl._INJECTION_PHRASES.search(log):
        raise GateFail("log: instruction-like text")
    return pages, log


def _load_digest(path):
    try:
        d = sl.read_json(path)
    except (OSError, ValueError) as exc:
        raise GateFail(f"digest is not JSON: {exc}")
    run_id = d.get("run_id") if isinstance(d, dict) else None
    if not isinstance(run_id, str) or not run_id.strip():
        raise GateFail("digest has no run_id")
    return run_id.strip(), d.get("terminal")


def build(lib_root, repo, pages, run_id, now):
    """{slug: text} of every page to write, plus (created, patched) slug
    lists. Raises GateFail; writes nothing."""
    creates = [p for p in pages if isinstance(p, dict) and p.get("op") == "create"]
    patches = [p for p in pages if isinstance(p, dict) and p.get("op") == "patch"]
    for n, p in enumerate(pages):
        if not isinstance(p, dict):
            raise GateFail(f"page {n} is not an object")
        if p.get("op") not in ("create", "patch"):
            raise GateFail(f"page {n}: unknown op {p.get('op')!r}")
    if len(creates) > MAX_CREATES:
        raise GateFail(f"{len(creates)} creates, cap {MAX_CREATES} per run")
    if len(patches) > MAX_PATCHES:
        raise GateFail(f"{len(patches)} patches, cap {MAX_PATCHES} per run")
    seen = set()
    for p in pages:
        slug = p.get("slug")
        if slug in seen:
            raise GateFail(f"slug {slug!r} appears more than once")
        seen.add(slug)
    known = _known_runs(lib_root, repo, run_id)
    out, created, patched = {}, [], []
    for p in pages:
        slug = p.get("slug")
        if not _slug_ok(slug):
            raise GateFail(f"invalid slug {slug!r}")
        current = _read_page(lib_root, repo, slug)
        if p["op"] == "create":
            if current is not None:
                raise GateFail(f"create: page {slug} already exists")
            text = _build_create(p, run_id, now)
            created.append(slug)
        else:
            if current is None:
                raise GateFail(f"patch: page {slug} does not exist")
            text = _build_patch(p, current, run_id, now)
            patched.append(slug)
        errs = sl.lint_pattern(slug, text)
        if errs:
            raise GateFail(f"page {slug}: {errs[0]}")
        _check_evidence(slug, text, known)
        out[slug] = text
    return out, created, patched


def _commit(lib_root, repo, message):
    p = sl.paths(lib_root, repo)
    if sl.git_toplevel(p["root"]) is None:
        print("LIBRARY_COMMIT=SKIP no git")
        return None
    sha = sl.git_commit(lib_root, repo, message)
    if sha is None:
        print("LIBRARY_COMMIT=SKIP nothing to commit")
        return None
    print(f"LIBRARY_COMMIT=OK sha={sha[:12]}")
    return sha


def _summary(out, payload):
    if out:
        sl.write_json_atomic(out, payload)


def cmd_apply(args):
    summary = {"result": "FAIL", "created": [], "patched": [], "reason": "", "run_id": None}
    try:
        sl.check_repo_name(args.repo)
        p = sl.paths(args.lib, args.repo)
        if not sl.skeleton_present(args.lib, args.repo):
            raise GateFail(f"library/{args.repo} skeleton missing")
        run_id, _terminal = _load_digest(args.digest)
        summary["run_id"] = run_id
        pages, log = _load_patch(args.patch)
        if sl.git_dirty(args.lib, args.repo):
            raise GateFail(f"library/{args.repo} is dirty in git; commit or discard before apply")
        now = sl.utc_now()
        built, created, patched = build(args.lib, args.repo, pages, run_id, now)
    except GateFail as exc:
        summary["reason"] = str(exc)
        _summary(args.out, summary)
        print(f"WIKI_GATE=FAIL {exc}")
        return 1
    except GATE_ERRORS as exc:
        summary["reason"] = f"{type(exc).__name__}: {exc}"
        _summary(args.out, summary)
        print(f"WIKI_GATE=FAIL {type(exc).__name__}: {exc}")
        return 1

    summary.update(result="PASS", created=created, patched=patched)
    if args.dry_run:
        summary["dry_run"] = True
        _summary(args.out, summary)
        print(f"WIKI_GATE=PASS created={len(created)} patched={len(patched)} "
              "index_regenerated=no log_appended=no dry_run=yes")
        return 0
    for slug, text in built.items():
        sl.write_text_atomic(sl.pattern_path(args.lib, args.repo, slug), text)
    sl.regenerate_index_md(args.lib, args.repo)
    sl.append_log(args.lib, args.repo,
                  f"wiki: {log} (run {run_id}; created={len(created)} patched={len(patched)})", at=now)
    sha = _commit(args.lib, args.repo, f"wiki({args.repo}): {log}")
    summary["commit"] = sha
    _summary(args.out, summary)
    print(f"WIKI_GATE=PASS created={len(created)} patched={len(patched)} "
          "index_regenerated=yes log_appended=yes")
    return 0


def cmd_quarantine(args):
    try:
        sl.check_repo_name(args.repo)
        reason = " ".join(str(args.reason or "").split())
        if not reason:
            raise GateFail("--reason is required")
        if sl._INJECTION_PHRASES.search(reason):
            raise GateFail("reason: instruction-like text")
        if not _slug_ok(args.slug):
            raise GateFail(f"invalid slug {args.slug!r}")
        current = _read_page(args.lib, args.repo, args.slug)
        if current is None:
            raise GateFail(f"page {args.slug} does not exist")
        meta, _ = sl.parse_pattern(current)
        previous = meta.get("status")
        if previous == "contested":
            raise GateFail(f"page {args.slug} is already contested")
        if sl.git_dirty(args.lib, args.repo):
            raise GateFail(f"library/{args.repo} is dirty in git; commit or discard before quarantine")
        now = sl.utc_now()
        text = sl.apply_section_ops(current, [{"op": "set_meta", "key": "status", "value": "contested"}])
        errs = sl.lint_pattern(args.slug, text)
        if errs:
            raise GateFail(f"page {args.slug}: {errs[0]}")
    except GateFail as exc:
        print(f"WIKI_QUARANTINE=FAIL {exc}")
        return 1
    except GATE_ERRORS as exc:
        print(f"WIKI_QUARANTINE=FAIL {type(exc).__name__}: {exc}")
        return 1
    sl.write_text_atomic(sl.pattern_path(args.lib, args.repo, args.slug), text)
    sl.regenerate_index_md(args.lib, args.repo)
    sl.append_log(args.lib, args.repo,
                  f"quarantine: {args.slug} {previous} -> contested ({reason})", at=now)
    sl.record_impact(args.lib, args.repo, "pattern_quarantined", "-", "-", args.evolve_run or "-",
                     reason=reason, details={"slug": args.slug, "previous_status": previous}, at=now)
    _commit(args.lib, args.repo, f"wiki({args.repo}): quarantine {args.slug}")
    print(f"WIKI_QUARANTINE=OK slug={args.slug} status=contested")
    return 0


def cmd_regen_index(args):
    try:
        sl.check_repo_name(args.repo)
        rows = sl.regenerate_index_md(args.lib, args.repo)
    except GATE_ERRORS as exc:
        print(f"WIKI_INDEX=FAIL {exc}")
        return 1
    print(f"WIKI_INDEX=OK rows={rows}")
    return 0


def cmd_lint(args):
    try:
        sl.check_repo_name(args.repo)
        d = sl.paths(args.lib, args.repo)["patterns_dir"]
    except sl.LibraryError as exc:
        print(f"WIKI_LINT=FAIL {exc}")
        return 1
    names = sorted(n for n in os.listdir(d) if n.endswith(".md")) if os.path.isdir(d) else []
    for name in names:
        slug = name[:-3]
        with open(os.path.join(d, name), encoding="utf-8") as fh:
            errs = sl.lint_pattern(slug, fh.read())
        if errs:
            print(f"WIKI_LINT=FAIL slug={slug} {errs[0]}")
            return 1
    print(f"WIKI_LINT=OK pages={len(names)}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply")
    a.add_argument("repo")
    a.add_argument("patch")
    a.add_argument("--lib", required=True)
    a.add_argument("--digest", required=True)
    a.add_argument("--out")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_apply)

    q = sub.add_parser("quarantine")
    q.add_argument("repo")
    q.add_argument("slug")
    q.add_argument("--lib", required=True)
    q.add_argument("--reason", required=True)
    q.add_argument("--evolve-run")
    q.set_defaults(fn=cmd_quarantine)

    r = sub.add_parser("regen-index")
    r.add_argument("repo")
    r.add_argument("--lib", required=True)
    r.set_defaults(fn=cmd_regen_index)

    l = sub.add_parser("lint")
    l.add_argument("repo")
    l.add_argument("--lib", required=True)
    l.set_defaults(fn=cmd_lint)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
