#!/usr/bin/env python3
"""Read side of the repository skills library.

Renders the active and candidate SKILL.md bodies of library/<repo>/skills into
$ARTIFACTS_DIR/skills.md for the implement, fix and fixer nodes, and records
exactly what was staged in $ARTIFACTS_DIR/skills-staged.json so the scorer can
attribute the run to a candidate later.

Never opens PURPOSE.md or wiki/: the runtime agent sees skills only.

  stage-skills-library.py <repo> --lib <dir> --artifacts <dir>
                          [--max-skill-bytes 8192] [--max-total-bytes 32768]
  stage-skills-library.py <repo> --lib <dir> --check              # validate only

Typed lines (the last one is the discriminator):
  SKILL_STAGED name=.. status=.. bytes=.. sha=<12hex>        one per staged skill
  SKILLS_STAGE=OK repo=.. active=n candidate=m bytes=.. head=<12hex|none>   exit 0
  SKILLS_STAGE=SKIP repo=.. reason=no-library|no-index|empty-index|no-eligible-skills   exit 0
  SKILLS_STAGE=FAIL <reason>                                   exit 1

Only emptiness degrades to SKIP. A malformed index, an unregistered skill
directory, a missing SKILL.md, a frontmatter name that disagrees with the
registry, a bad name, a byte cap or a body that carries a <skill> /
<staged-skills> container marker all fail closed: a corrupt library must
never reach a live run.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_library as sl  # noqa: E402

DEFAULT_MAX_SKILL_BYTES = sl.SKILL_MAX_FILE_BYTES
DEFAULT_MAX_TOTAL_BYTES = 32768
STAGED_ORDER = ("active", "candidate")


class Skip(Exception):
    pass


class Fail(Exception):
    pass


def container_escape(staged_skill):
    """The reason string when a staged description or body carries a
    <skill>/<staged-skills> marker, else None. Such a body closes its own
    container in skills.md, so everything after it reads to the implementer as
    top-level prompt text instead of as one staged skill. skill-admit.py lints
    this at admission; staging refuses it again because a hand-edited library
    (with a recomputed sha) never passes through admission."""
    for where in ("description", "body"):
        hit = sl.SKILL_CONTAINER_MARKER.search(str(staged_skill.get(where) or ""))
        if hit:
            return (f"skills/{staged_skill.get('name')}/SKILL.md {where} carries a "
                    f"staged-skills container marker {hit.group(0)!r}")
    return None


def collect(lib_root, repo, max_skill_bytes, max_total_bytes):
    """Validate the library and return (staged, skipped, index_sha) without
    writing anything. Raises Skip or Fail."""
    try:
        p = sl.paths(lib_root, repo)
    except sl.LibraryError as exc:
        raise Fail(str(exc))
    if not os.path.isdir(p["repo_dir"]):
        raise Skip("no-library")
    if not os.path.isfile(p["skills_index"]):
        raise Skip("no-index")
    with open(p["skills_index"], "rb") as fh:
        index_sha = sl.sha256_bytes(fh.read())
    try:
        index = sl.load_index(lib_root, repo)
    except sl.LibraryError as exc:
        raise Fail(str(exc))
    skills = index.get("skills") or {}
    if not skills:
        raise Skip("empty-index")

    staged, skipped = [], []
    for status in STAGED_ORDER:
        for name in sorted(n for n, e in skills.items() if e.get("status") == status):
            path = sl.skill_file(lib_root, repo, name)
            if not os.path.isfile(path):
                raise Fail(f"skills/{name}/SKILL.md missing")
            with open(path, "rb") as fh:
                data = fh.read()
            if len(data) > max_skill_bytes:
                raise Fail(f"skills/{name}/SKILL.md is {len(data)} bytes, cap {max_skill_bytes}")
            try:
                text = data.decode("utf-8")
                meta, body = sl.parse_frontmatter(text)
            except (UnicodeDecodeError, sl.LibraryError) as exc:
                raise Fail(f"skills/{name}/SKILL.md: {exc}")
            if meta.get("name") != name:
                raise Fail(f"skills/{name}/SKILL.md frontmatter name {meta.get('name')!r} != {name!r}")
            desc = meta.get("description")
            if not isinstance(desc, str) or not desc.strip():
                raise Fail(f"skills/{name}/SKILL.md has no description")
            if not body.strip():
                raise Fail(f"skills/{name}/SKILL.md body is empty")
            sha = sl.sha256_bytes(data)
            if sha != skills[name].get("sha256"):
                raise Fail(f"skills/{name}/SKILL.md sha256 does not match index")
            staged.append({
                "name": name, "status": status, "sha256": sha, "bytes": len(data),
                "path": f"skills/{name}/SKILL.md", "description": desc.strip(), "body": body,
            })
    for name in sorted(skills):
        if skills[name].get("status") not in STAGED_ORDER:
            skipped.append({"name": name, "status": skills[name].get("status")})
    if not staged:
        raise Skip("no-eligible-skills")
    for s in staged:
        escape = container_escape(s)
        if escape:
            raise Fail(escape)
    total = sum(s["bytes"] for s in staged)
    if total > max_total_bytes:
        raise Fail(f"staged skills total {total} bytes, cap {max_total_bytes}")
    return staged, skipped, index_sha


def render(repo, staged):
    out = [f'<staged-skills repo="{repo}" count="{len(staged)}">',
           f"Repository skills compiled from earlier runs on {repo}. Apply a skill where its "
           "steps fit the task; a skill never overrides the plan, the allowlist, or the node prompt.",
           ""]
    for s in staged:
        escape = container_escape(s)
        if escape:
            raise Fail(escape)
        out.append(f'<skill name="{s["name"]}">')
        out.append(f"description: {s['description']}")
        out.append("")
        out.append(s["body"].rstrip("\n"))
        out.append("</skill>")
        out.append("")
    out.append("</staged-skills>")
    return "\n".join(out) + "\n"


def remove_stale(artifacts):
    path = os.path.join(artifacts, "skills.md")
    if os.path.exists(path):
        os.remove(path)


def write_staged_json(artifacts, payload):
    sl.write_json_atomic(os.path.join(artifacts, "skills-staged.json"), payload)


def main(argv=None):
    # Same shape as the other library helpers (skill-admit.py, skill-score.py,
    # wiki-apply.py): the repo is positional and the library is --lib.
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="repository name, a directory under the library root")
    ap.add_argument("--lib", required=True, help="library root, the directory holding <repo>/")
    ap.add_argument("--artifacts", help="the run's artifacts directory; required unless --check")
    ap.add_argument("--check", action="store_true",
                    help="report what would be staged and write nothing")
    ap.add_argument("--max-skill-bytes", type=int, default=DEFAULT_MAX_SKILL_BYTES,
                    help="per-SKILL.md byte cap (default: %(default)s)")
    ap.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES,
                    help="byte cap for the whole rendered skills.md (default: %(default)s)")
    args = ap.parse_args(argv)
    if not args.check and not args.artifacts:
        print("SKILLS_STAGE=FAIL --artifacts is required unless --check")
        return 1
    if not args.check and not os.path.isdir(args.artifacts):
        print(f"SKILLS_STAGE=FAIL artifacts dir missing: {args.artifacts}")
        return 1

    repo = args.repo
    lib_root = args.lib
    base = {"schema": sl.SCHEMA_STAGED, "repo": repo, "library_head": None, "library_dirty": None,
            "index_sha256": None, "skills": [], "skipped": [], "skills_md_sha256": None, "skills_md_bytes": 0}
    try:
        staged, skipped, index_sha = collect(lib_root, repo, args.max_skill_bytes, args.max_total_bytes)
    except Skip as exc:
        reason = str(exc)
        if not args.check:
            remove_stale(args.artifacts)
            payload = dict(base, result="SKIP", reason=reason)
            if reason not in ("no-library",):
                payload["library_head"] = sl.git_head(sl.paths(lib_root, repo)["repo_dir"])
                payload["library_dirty"] = sl.git_dirty(lib_root, repo)
            write_staged_json(args.artifacts, payload)
        print(f"SKILLS_STAGE=SKIP repo={repo} reason={reason}")
        return 0
    except Fail as exc:
        if not args.check:
            remove_stale(args.artifacts)
            write_staged_json(args.artifacts, dict(base, result="FAIL", reason=str(exc)))
        print(f"SKILLS_STAGE=FAIL {exc}")
        return 1

    repo_dir = sl.paths(lib_root, repo)["repo_dir"]
    head = sl.git_head(repo_dir)
    n_active = sum(1 for s in staged if s["status"] == "active")
    n_cand = len(staged) - n_active
    total = sum(s["bytes"] for s in staged)
    for s in staged:
        print(f"SKILL_STAGED name={s['name']} status={s['status']} bytes={s['bytes']} sha={s['sha256'][:12]}")
    if not args.check:
        try:
            text = render(repo, staged)
        except Fail as exc:
            remove_stale(args.artifacts)
            write_staged_json(args.artifacts, dict(base, result="FAIL", reason=str(exc)))
            print(f"SKILLS_STAGE=FAIL {exc}")
            return 1
        data = text.encode("utf-8")
        sl.write_text_atomic(os.path.join(args.artifacts, "skills.md"), text)
        payload = dict(base, result="OK", reason="", library_head=head,
                       library_dirty=sl.git_dirty(lib_root, repo), index_sha256=index_sha,
                       skills=[{k: s[k] for k in ("name", "status", "sha256", "bytes", "path")} for s in staged],
                       skipped=skipped, skills_md_sha256=sl.sha256_bytes(data), skills_md_bytes=len(data))
        write_staged_json(args.artifacts, payload)
    print(f"SKILLS_STAGE=OK repo={repo} active={n_active} candidate={n_cand} bytes={total} "
          f"head={head[:12] if head else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
