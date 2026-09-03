#!/usr/bin/env python3
"""Fail-closed disclosure gate for generated lite-lane PR bodies."""

import sys
import json
from pathlib import Path


LANES = {
    "api": {
        "name": "full-sdlc-api-lite",
        "omissions": "Lite omissions: one code-review round; no planning critic, doc review, premise verification, deslop pass, or reader audit.",
        "headings": (
            "## Summary",
            "## Lane",
            "## Reviewer-unverified fixes",
            "## Known Residuals",
            "## Post-Deploy Monitoring & Validation",
        ),
    },
    "bugfix": {
        "name": "bugfix-lite",
        "omissions": "Lite omissions: no production evidence, blind chain verification, planning critic, live experiment, deslop pass, HTTP smoke, or in-app smoke matrix.",
        "headings": (
            "## Summary",
            "## Lane",
            "## Root Cause",
            "## Proof",
            "## Known Residuals",
            "## Post-Deploy Monitoring & Validation",
        ),
    },
}


def fail(reason: str) -> "NoReturn":
    print(f"PRBODY_GATE=FAIL {reason}")
    raise SystemExit(1)


def read_required(ad: Path, name: str) -> str:
    path = ad / name
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        fail(f"missing {name}: {exc}")
    if not text:
        fail(f"empty {name}")
    return text


def last_line(ad: Path, name: str) -> str:
    lines = [line.strip() for line in read_required(ad, name).splitlines() if line.strip()]
    if not lines:
        fail(f"empty {name}")
    return lines[-1]


def require(body: str, needle: str, reason: str) -> None:
    if needle not in body:
        fail(reason)


def read_classification(ad: Path) -> dict:
    try:
        return json.loads((ad / "fix-classification.json").read_text(encoding="utf-8"))
    except OSError as exc:
        fail(f"missing fix-classification.json: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"malformed fix-classification.json: {exc}")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in LANES:
        fail("usage: check-lite-prbody.py <api|bugfix> <artifacts-dir>")
    lane, ad = sys.argv[1], Path(sys.argv[2])
    contract = LANES[lane]
    body = read_required(ad, "pr-body.md")

    require(body, f"Lane: {contract['name']}", f"lane marker {contract['name']} missing")
    require(body, contract["omissions"], "exact lite-omissions disclosure missing")
    for heading in contract["headings"]:
        require(body, heading, f"heading missing: {heading}")

    envelope = read_required(ad, "envelope-post.txt")
    require(body, envelope, "envelope-post.txt is not present verbatim")

    smoke = read_required(ad, "smoke-result.txt")
    require(body, smoke, "smoke-result.txt is not present verbatim")

    if lane == "api":
        unreviewed = ad / "lite-fixes-unreviewed.txt"
        if unreviewed.is_file():
            require(body, read_required(ad, unreviewed.name),
                    "lite-fixes-unreviewed.txt is not present verbatim")
            require(body, "NOT re-read by a reviewer", "unreviewed-fix warning missing")
        else:
            require(body, "None: the review round landed no fixes.",
                    "no-fixer disclosure missing")
    else:
        classification = read_classification(ad)
        implementation = classification.get("implementation_result")
        ticket = classification.get("ticket_disposition")
        scope = classification.get("approval_scope")
        closure = str(bool(classification.get("ticket_closure_allowed"))).lower()
        if implementation != "FULL_FIX":
            print(
                "ROUTE=FULL reason=classification "
                f"implementation={implementation} ticket={ticket} approval_scope={scope}"
            )
            raise SystemExit(1)
        for needle in (
            f"implementation_result={implementation}",
            f"ticket_disposition={ticket}",
            f"approval_scope={scope}",
            f"ticket_closure_allowed={closure}",
        ):
            require(body, needle, f"classification banner missing: {needle}")
        if not smoke.startswith("SMOKE=SKIP lane=bugfix-lite"):
            fail("bugfix-lite smoke-result.txt is not the typed skip contract")
        smoke_matrix = read_required(ad, "smoke-matrix-result.txt")
        if not smoke_matrix.startswith("SMOKE_MATRIX=SKIP lane=bugfix-lite"):
            fail("bugfix-lite smoke-matrix-result.txt is not the typed skip contract")
        require(body, smoke_matrix, "smoke-matrix-result.txt is not present verbatim")
        for name in ("negcontrol-postgreen.txt", "negcontrol-exit.txt"):
            require(body, last_line(ad, name), f"{name} final line is not present verbatim")

    print(f"PRBODY_GATE=PASS lane={contract['name']}")


if __name__ == "__main__":
    main()
