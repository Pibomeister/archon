#!/usr/bin/env python3
"""W6 gate helper: write the wrapper's own typed summary artifact.
Usage: write-review-summary.py <out.json> <verdict> <true|false-degraded> [residual_count]"""
import json
import sys

out, verdict, degraded = sys.argv[1], sys.argv[2], sys.argv[3] == "true"
residual = int(sys.argv[4]) if len(sys.argv) > 4 else -1
with open(out, "w", encoding="utf-8") as f:
    json.dump({"verdict": verdict, "residual_count": residual, "degraded": degraded}, f)
print(f"SUMMARY_WRITTEN={out}")
