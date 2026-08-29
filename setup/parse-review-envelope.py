#!/usr/bin/env python3
"""Parse a ce-code-review return under EITHER skill contract.

Old contract (CE 3.2.0 headless): markdown envelope containing 'Review complete',
optionally 'Code review degraded (headless mode)', and a 'Verdict: <enum>' line.
New contract (CE >= ~3.19 mode:agent, mode:headless aliased): ONE raw JSON object
with fields status (complete|failed|degraded|skipped) and verdict (same enum).

Usage: parse-review-envelope.py <envelope-file>
Prints shell-eval-able lines (values are shlex-quoted: every enum verdict
contains a space, and an unquoted `VERDICT=Ready with fixes` makes `eval`
run the command `with` — observed live 2026-08-28, run d3aa3b55 round 2):
  G1=PASS|FAIL   terminal signal present (review actually completed)
  G2=PASS|FAIL   not degraded
  VERDICT=<enum or empty>
  VSRC=json|envelope|none
"""

import json
import re
import shlex
import sys

ENUM = ("Ready to merge", "Ready with fixes", "Not ready")


def find_json(text):
    """First parseable JSON object carrying status or verdict."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and (
            str(obj.get("status", "")).lower() in ("complete", "degraded") or obj.get("verdict") in ENUM
        ):
            return obj
    except ValueError:
        pass
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        # Only a dict that carries the envelope's own vocabulary counts. Review
        # prose routinely embeds persona JSON such as {"status": "ok"} — observed
        # live 2026-08-28 (run d3aa3b55): that object won the search, VERDICT came
        # back empty, and G1 failed a review whose text said "Verdict: Ready to
        # merge". Anything else falls through to the text contract below.
        status = str(obj.get("status", "")).lower()
        if status in ("complete", "degraded") or obj.get("verdict") in ENUM:
            return obj
    return None


def main():
    text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
    obj = find_json(text)
    if obj is not None:
        status = str(obj.get("status", "")).lower()
        g1 = "PASS" if status in ("complete", "degraded") or (not status and obj.get("verdict")) else "FAIL"
        g2 = "FAIL" if status == "degraded" else "PASS"
        v = obj.get("verdict") if obj.get("verdict") in ENUM else ""
        print(f"G1={shlex.quote(g1)}")
        print(f"G2={shlex.quote(g2)}")
        print(f"VERDICT={shlex.quote(v)}")
        print("VSRC=json")
        return
    g1 = "PASS" if "Review complete" in text else "FAIL"
    g2 = "FAIL" if "Code review degraded (headless mode)" in text else "PASS"
    m = re.search(r"Verdict: (Ready to merge|Ready with fixes|Not ready)", text)
    v = m.group(1) if m else ""
    print(f"G1={shlex.quote(g1)}")
    print(f"G2={shlex.quote(g2)}")
    print(f"VERDICT={shlex.quote(v)}")
    print("VSRC=envelope" if (m or g1 == "PASS") else "VSRC=none")


if __name__ == "__main__":
    main()
