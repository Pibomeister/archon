#!/usr/bin/env python3
"""Stand-in for setup/ledger.py, on B's exact CLI.

It mirrors the one behaviour round-state.py depends on -- `closure` exits 1 when
any P0/P1 in round-N/ledger.json is not `closed` -- and records every call in
<artifacts>/ledger-calls.txt so a test can assert the merge boundaries and their
idempotency. Tests drive the decision table by writing round-N/ledger.json, the
same file the real ledger writes, rather than through a stub-only side channel.

Point round-state.py at it with ARCHON_LEDGER_PY.

  ledger.py <artifacts> merge-envelope <round> <envelope> [--repo <path>]
  ledger.py <artifacts> merge-fixer <round> <fixer-result.json> <attempt> [--repo <path>]
  ledger.py <artifacts> copy-forward <from-round> <to-round>
  ledger.py <artifacts> closure <round>
  ledger.py <artifacts> residuals <round> <out.json>
"""
import json
import sys
from pathlib import Path

BLOCKING = ("P0", "P1")


def entries(artifacts, n):
    path = Path(artifacts) / f"round-{n}" / "ledger.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def main(argv):
    artifacts, command = Path(argv[0]), argv[1]
    rest = [a for a in argv[2:] if a != "--repo"]
    with (artifacts / "ledger-calls.txt").open("a", encoding="utf-8") as log:
        log.write(" ".join(argv) + "\n")
    if command == "closure":
        n = rest[0]
        rows = entries(artifacts, n)
        unclosed = [e for e in rows
                    if e.get("severity") in BLOCKING and e.get("state") != "closed"]
        print(f"CLOSURE round={n} "
              f"closed={sum(1 for e in rows if e.get('state') == 'closed')} "
              f"open={sum(1 for e in rows if e.get('state') in ('open', 'applied', 'incomplete'))} "
              f"regressed={sum(1 for e in rows if e.get('state') == 'regressed')} "
              f"new={sum(1 for e in rows if str(e.get('round')) == str(n))}")
        for e in unclosed:
            print(f"CLOSURE_UNCLOSED id={e.get('id')} severity={e.get('severity')} "
                  f"state={e.get('state')} round={e.get('round')}")
        return 1 if unclosed else 0
    if command == "residuals":
        rows = [e for e in entries(artifacts, rest[0])
                if e.get("severity") in ("P2", "P3")
                and e.get("state") in ("deferred", "pinned", "waived")]
        Path(rest[1]).write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        print(f"RESIDUALS round={rest[0]} count={len(rows)}")
        return 0
    if command == "copy-forward":
        source = artifacts / f"round-{rest[0]}" / "ledger.json"
        target = artifacts / f"round-{rest[1]}" / "ledger.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"LEDGER_COPY round={rest[1]} from={rest[0]}")
        return 0
    print(f"LEDGER_MERGE {command} round={rest[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
