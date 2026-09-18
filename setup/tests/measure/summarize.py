#!/usr/bin/env python3
"""Re-read the bench's saved envelopes and print the RESULTS table.

The envelopes are the measurement's durable output; the per-run JSON is a
derived view of them. Keeping the extraction separate means a fix to the finding
parser -- and there was one, the trio emits a bare JSON array where the schema's
wrapper object was expected -- can be applied to runs that already happened
instead of re-billing them.

Usage: summarize.py <runs-dir> [label ...]
"""
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("review_bench", HERE / "review-bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def main():
    runs = Path(sys.argv[1])
    labels = sys.argv[2:] or sorted(p.name for p in runs.iterdir() if p.is_dir())
    print(f"| mode | runs | p50 min | p90 min | verdicts | P0/P1 found | cost USD |")
    print(f"|---|---:|---:|---:|---|---:|---:|")
    detail = []
    for label in labels:
        d = runs / label
        rows = []
        for f in sorted(d.glob("run-*.json")):
            row = json.loads(f.read_text())
            env = d / f"{f.stem}-envelope.txt"
            if env.is_file():
                row["findings"] = bench.findings_in(env.read_text(errors="replace"))
                row["p0p1"] = [x for x in row["findings"] if x["severity"] in ("P0", "P1")]
            rows.append(row)
        mins = [r["minutes"] for r in rows if r.get("minutes")]
        costs = [r["cost_usd"] for r in rows if r.get("cost_usd")]
        p50, p90 = bench.pct(mins, 50), bench.pct(mins, 90)
        print(f"| {label} | {len(mins)}/{len(rows)} | "
              f"{'-' if p50 is None else round(p50, 1)} | "
              f"{'-' if p90 is None else round(p90, 1)} | "
              f"{', '.join(sorted({r.get('verdict') or '(none)' for r in rows}))} | "
              f"{min((len(r['p0p1']) for r in rows), default=0)}-"
              f"{max((len(r['p0p1']) for r in rows), default=0)} | "
              f"{'-' if not costs else round(sum(costs), 2)} |")
        detail.append((label, rows))

    for label, rows in detail:
        print(f"\n### {label}")
        for r in rows:
            print(f"- run {r['run']}: "
                  f"{'TIMEOUT' if r.get('timed_out') else str(round(r['minutes'], 1)) + ' min'}"
                  f" wall {round(r['wall_seconds'] / 60, 1)}"
                  f" verdict=[{r.get('verdict')}]"
                  f" footer={r.get('has_footer')}"
                  f" input_ok={r.get('input_line_matches')}"
                  f" head_ok={r.get('head_line_matches')}"
                  f" tree_moved={r.get('tree_moved')}"
                  f" findings={len(r.get('findings') or [])}"
                  f" turns={r.get('num_turns')}"
                  f" cost={r.get('cost_usd')}")
            for f in r.get("p0p1") or []:
                print(f"    {f['severity']} {f['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
