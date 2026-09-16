#!/usr/bin/env python3
"""Stand-in for setup/ledger.py while B's implementation lands.

It records every call in <artifacts>/ledger-calls.txt so a test can assert the
merge boundaries and their idempotency, and answers `closure` from
<artifacts>/stub-closure.json so a test can drive each row of the decision
table. Point round-state.py at it with ARCHON_LEDGER_PY.

CLI (the contract round-state.py depends on):
  ledger.py merge-envelope <artifacts> <round> --envelope <p> --mode <m> --head <sha>
  ledger.py merge-fixer    <artifacts> <round> --result <p> --attempt <k> --head <sha>
  ledger.py copy-forward   <artifacts> <from-round> <to-round>
  ledger.py closure        <artifacts> <round> --head <sha> [--json]
  ledger.py residuals      <artifacts> <round> --out <p>
"""
import json
import sys
from pathlib import Path

DEFAULT_CLOSURE = {
    "closed": 0, "open": 0, "regressed": 0, "new": 0,
    "pin_conflict": [], "filed": [], "blockers_open": 0,
    "new_blockers": 0, "closure_ok": True,
}


def main(argv):
    command, artifacts = argv[0], Path(argv[1])
    with (artifacts / "ledger-calls.txt").open("a", encoding="utf-8") as log:
        log.write(" ".join(argv) + "\n")
    if command == "closure":
        data = dict(DEFAULT_CLOSURE)
        override = artifacts / "stub-closure.json"
        if override.is_file():
            data.update(json.loads(override.read_text(encoding="utf-8")))
        print(json.dumps(data))
    elif command == "residuals":
        out = Path(argv[argv.index("--out") + 1])
        out.write_text(json.dumps({"residuals": []}) + "\n", encoding="utf-8")
    elif command == "copy-forward":
        source = artifacts / f"round-{argv[2]}" / "ledger.json"
        target = artifacts / f"round-{argv[3]}" / "ledger.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
