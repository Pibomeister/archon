#!/usr/bin/env python3
"""Cross-repo findings and the operator's record that each one was filed.

A fixer's `cross_repo` entry is a defect owned by another repository. It is not
waivable and not fixable in this run, so converge and exit-gate stop on it. The
only thing that resolves one is a HUMAN recording where it was filed, in
`<artifacts>/cross-repo-filed.json` (same class of act as accept-residuals.txt;
no workflow node may write it):

  [{"key": "<from this script>", "filed": "https://...", "by": "<who>"}]

The key is a digest of producer_repo plus the finding text with whitespace
collapsed, so an acknowledgement matches that exact finding and nothing else. A
later round that words the finding differently mints a new key and stops again.

Usage:
  cross-repo-keys.py <artifacts>                 operator listing + JSON skeleton
  cross-repo-keys.py <artifacts> --gate <fixer-result.json>
      exit 0 when every cross_repo entry is acknowledged (prints one
      CROSS_REPO_ACKED line each); otherwise writes the unacknowledged entries
      to cross-repo-findings.json and exits 1 with `count=N repos=a,b` last.
  cross-repo-keys.py <artifacts> --prbody        markdown section, empty if none
"""
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ACK_FILE = "cross-repo-filed.json"


def key_of(entry):
    text = " ".join(str(entry.get("finding", "")).split())
    return hashlib.sha256(f"{entry.get('producer_repo', '')}\n{text}".encode("utf-8")).hexdigest()[:16]


def load_acks(artifacts):
    """{key: entry} of valid acknowledgements, and the problems with the rest.
    An unreadable file acknowledges nothing."""
    path = Path(artifacts) / ACK_FILE
    if not path.exists():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, [f"{ACK_FILE} unreadable: {exc}"]
    if not isinstance(data, list):
        return {}, [f"{ACK_FILE} must be a JSON list"]
    acks, problems = {}, []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            problems.append(f"entry {i} is not an object")
            continue
        url = urlparse(str(item.get("filed") or "").strip())
        if url.scheme not in ("http", "https") or not url.netloc:
            problems.append(f"entry {i} key={item.get('key')} filed is not an http(s) URL")
        elif not str(item.get("by") or "").strip():
            problems.append(f"entry {i} key={item.get('key')} by is empty")
        elif not str(item.get("key") or "").strip():
            problems.append(f"entry {i} key is empty")
        else:
            acks[str(item["key"]).strip()] = item
    return acks, problems


def cross_repo_of(fixer_result):
    """The cross_repo partition, [] when the file is absent; raises when unreadable."""
    path = Path(fixer_result)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("cross_repo") or []


def gate(artifacts, fixer_result):
    try:
        entries = cross_repo_of(fixer_result)
    except (OSError, ValueError, AttributeError) as exc:
        print(f"CROSS_REPO_ACK=FAIL fixer-result.json unreadable: {exc}")
        print("count=? repos=?")
        return 1
    acks, problems = load_acks(artifacts)
    for problem in problems:
        print(f"CROSS_REPO_ACK=FAIL {problem}")
    open_entries = []
    for entry in entries:
        key = key_of(entry)
        ack = acks.get(key)
        if ack:
            print(f"CROSS_REPO_ACKED key={key} repo={entry.get('producer_repo')} "
                  f"filed={ack['filed']} by={ack['by']}")
        else:
            open_entries.append({**entry, "key": key})
    if not open_entries:
        return 0
    Path(artifacts, "cross-repo-findings.json").write_text(json.dumps(open_entries, indent=1), encoding="utf-8")
    repos = ",".join(sorted({str(e.get("producer_repo", "?")) for e in open_entries}))
    print(f"count={len(open_entries)} repos={repos}")
    return 1


def round_entries(artifacts):
    """Every cross_repo entry any round recorded, keyed, first occurrence wins."""
    found = {}
    rounds = sorted(Path(artifacts).glob("round-*/fixer-result.json"),
                    key=lambda p: int(p.parent.name[6:]) if p.parent.name[6:].isdigit() else 0)
    for path in rounds:
        try:
            entries = cross_repo_of(path)
        except (OSError, ValueError, AttributeError):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                found.setdefault(key_of(entry), entry)
    return found


def prbody(artifacts):
    acks, _ = load_acks(artifacts)
    rows = [(k, e, acks[k]) for k, e in round_entries(artifacts).items() if k in acks]
    if not rows:
        return ""
    out = ["## Cross-repo findings filed", ""]
    for key, entry, ack in rows:
        out.append(f"- [{entry.get('severity', '?')}] {entry.get('producer_repo')}: "
                   f"{' '.join(str(entry.get('finding', '')).split())} "
                   f"(filed: {ack['filed']}, by {ack['by']}, key `{key}`)")
    return "\n".join(out) + "\n"


def listing(artifacts):
    try:
        n = int(Path(artifacts, "round.txt").read_text(encoding="utf-8").strip())
        entries = cross_repo_of(Path(artifacts, f"round-{n}", "fixer-result.json"))
    except (OSError, ValueError, AttributeError) as exc:
        print(f"CROSS_REPO_KEYS=FAIL cannot read the final round's fixer-result.json: {exc}")
        return 1
    acks, problems = load_acks(artifacts)
    for problem in problems:
        print(f"CROSS_REPO_ACK=FAIL {problem}")
    if not entries:
        print(f"CROSS_REPO_KEYS=NONE round={n}")
        return 0
    skeleton = list(acks.values())
    for entry in entries:
        key = key_of(entry)
        summary = " ".join(str(entry.get("finding", "")).split())
        state = "acknowledged" if key in acks else "OPEN"
        print(f"key={key} severity={entry.get('severity', '?')} repo={entry.get('producer_repo')} "
              f"{state}: {summary[:140]}")
        if key not in acks:
            skeleton.append({"key": key, "filed": "", "by": ""})
    print(f"\nFile each OPEN finding against its repository, then write "
          f"{Path(artifacts) / ACK_FILE} by hand:")
    print(json.dumps(skeleton, indent=2))
    return 0


def main(argv):
    if len(argv) == 2:
        return listing(argv[1])
    if len(argv) == 4 and argv[2] == "--gate":
        return gate(argv[1], argv[3])
    if len(argv) == 3 and argv[2] == "--prbody":
        sys.stdout.write(prbody(argv[1]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
