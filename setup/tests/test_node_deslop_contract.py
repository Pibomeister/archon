#!/usr/bin/env python3
"""deslop-review-gate: a yagni finding on an approved plan contract symbol does not block.

Chain 42b42a13 (run aab1f254): the approved joint plan reserved an optional
`travelCandidates?: TravelCandidatesDto` field on the api's contract DTO for a
later ticket. The deslop reviewer filed it as yagni at confidence 100, the
fixer correctly declined to change the approved response shape, the round-2
reviewer re-flagged it, and the run stopped at DESLOP_ROUND_CAP. The stage's
contract-symbols.json (derived from the approval-bound plan) now answers that
mechanically: the gate keeps the finding at confidence 50 and prints
DESLOP_CONTRACT_DOWNGRADE.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

GUARDS = ("complexity", "tautological_tests", "yagni", "open_closed", "comments")
DTO = "dto/briefing-response.dto.ts"
DTO_TEXT = """export class TravelCandidatesDto {
  @ApiProperty({ type: [String] })
  candidateProfileIds: string[];
}

export class BriefingResponseDto {
  @ApiProperty({ type: TravelCandidatesDto, required: false })
  travelCandidates?: TravelCandidatesDto;

  unusedExtra?: string;
}

export class NotInThePlanDto {
  value: string;
}
"""
CONTRACT = {"contracts": [{"artifact": DTO, "symbols": ["BriefingResponseDto", "TravelCandidatesDto", "travelCandidates"]}]}


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True,
                          encoding="utf-8").stdout.strip()


def finding(line, file=DTO, guard="yagni", confidence=100):
    return {"guard": guard, "file": file, "line": line, "confidence": confidence,
            "evidence": "exported with no caller"}


class DeslopContractDowngrade(unittest.TestCase):
    def run_gate(self, findings, contract=CONTRACT, verdict="DIRTY"):
        tmp = Path(tempfile.mkdtemp(prefix="dcc-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wt = tmp / "wt"
        (wt / "dto").mkdir(parents=True)
        (wt / DTO).write_text(DTO_TEXT)
        sh("git init -q && git config user.email t@t && git config user.name t "
           "&& git add . && git commit -qm base", wt)
        ad = tmp / "ad"
        rd = ad / "deslop-round-1"
        rd.mkdir(parents=True)
        (ad / "deslop-round.txt").write_text("1\n")
        (ad / "params.json").write_text(json.dumps(
            {"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": str(wt)}))
        (rd / "slop.txt").write_text("SLOP=OK files=1\n")
        if contract is not None:
            (ad / "contract-symbols.json").write_text(json.dumps(contract))
        (ad / "deslop-review.json").write_text(json.dumps({
            "verdict": verdict,
            "coverage": {g: {"status": "assessed", "evidence": "read the diff"} for g in GUARDS},
            "findings": findings,
        }))
        head, tree = sh("git rev-parse HEAD", wt), sh("git write-tree", wt)
        (rd / "checkpoint-tree.txt").write_text(tree + "\n")
        (ad / "deslop-tree.txt").write_text(f"head={head}\nindex={tree}\ncheckpoint={tree}\n")
        body = runnable_body("full-sdlc-api", "deslop-review-gate", root=str(tmp))
        m = tmp / ".archon" / "setup"
        m.mkdir(parents=True)
        for p in (Path(__file__).resolve().parent.parent).iterdir():
            (m / p.name).symlink_to(p)
        return subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8",
                              env=dict(os.environ, ARTIFACTS_DIR=str(ad)), cwd=str(tmp))

    def test_a_declared_contract_field_does_not_block(self):
        p = self.run_gate([finding(8)])
        self.assertIn(f"DESLOP_CONTRACT_DOWNGRADE symbol=travelCandidates file={DTO}:8 confidence=100->50",
                      p.stdout, p.stdout + p.stderr)
        self.assertIn("DESLOP=CLEAN round=1", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_brace_line_inside_a_declared_contract_type_does_not_block(self):
        # The live finding pointed at the closing brace of TravelCandidatesDto.
        p = self.run_gate([finding(4)])
        self.assertIn("DESLOP_CONTRACT_DOWNGRADE symbol=TravelCandidatesDto", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_without_contract_symbols_the_finding_still_blocks(self):
        # Negative control: the same finding with no contract file is the old behaviour.
        p = self.run_gate([finding(8)], contract=None)
        self.assertNotIn("DESLOP_CONTRACT_DOWNGRADE", p.stdout)
        self.assertIn("DESLOP=DIRTY round=1 blocking=1", p.stdout, p.stdout + p.stderr)

    def test_an_unlisted_member_of_a_contract_type_still_blocks(self):
        p = self.run_gate([finding(10)])
        self.assertNotIn("DESLOP_CONTRACT_DOWNGRADE", p.stdout)
        self.assertIn(f"DESLOP_FINDING guard=yagni file={DTO}:10 confidence=100", p.stdout, p.stdout + p.stderr)

    def test_an_unlisted_type_in_the_contract_file_still_blocks(self):
        p = self.run_gate([finding(13)])
        self.assertNotIn("DESLOP_CONTRACT_DOWNGRADE", p.stdout)
        self.assertIn("DESLOP=DIRTY round=1 blocking=1", p.stdout, p.stdout + p.stderr)

    def test_the_downgrade_is_scoped_to_yagni_and_to_the_contract_file(self):
        p = self.run_gate([finding(8, guard="comments"), finding(8, file="dto/other.dto.ts")])
        self.assertNotIn("DESLOP_CONTRACT_DOWNGRADE", p.stdout)
        self.assertIn("DESLOP=DIRTY round=1 blocking=2", p.stdout, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
