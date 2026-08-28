#!/usr/bin/env python3
"""Query execution + reconciliation math for the backfill lane.

Shared by the census, sample, dry-run, and reconcile nodes so every number in
the run comes from ONE code path. All SQL this runner executes must be
read-only (SELECT/WITH, single statement) - it refuses anything else, which is
what lets the pre-gate nodes run against prod with RO credentials only.

Connection comes from the PG* environment variables the calling node exports
(same eval-block recipe as the bugfix lane's probe nodes). This runner never
touches Secrets Manager itself.

Subcommands (all take <plan.json> <artifacts_dir> first):
  census            population count, distribution, largest case, per-table
                    totals, negative-control baseline. Typed stops:
                    exit 4 = CLAIM_DIVERGED, exit 5 = EXTRAORDINARY_CLAIM
                    (unless <artifacts>/extraordinary-ack.txt exists).
  sample            15 raw rows + largest per-entity clusters -> sample-rows.txt
  dryrun            (kind=sql only) run dry_run_sql -> dryrun-count.txt
  dryrun-check      compare dryrun-count.txt vs census. exit 6 = DRYRUN_DIVERGED
  reconcile <n>     run reconcile_queries + negative control vs baseline, and
                    check dispositions sum == touched (=<n>) when the apply
                    recorded dispositions. exit 7 = RECONCILE_FAIL
"""

import json
import math
import os
import re
import subprocess
import sys

READONLY_RE = re.compile(r"(?is)^\s*(select|with)\b")
WRITE_KW_RE = re.compile(
    r"(?i)\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum)\b"
)
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def die(msg, code=1):
    print(msg)
    sys.exit(code)


def assert_readonly(sql, label):
    s = sql.strip().rstrip(";")
    if not READONLY_RE.match(s):
        die(f"CENSUS_RUNNER=FAIL {label}: must start with SELECT/WITH")
    if WRITE_KW_RE.search(s):
        die(f"CENSUS_RUNNER=FAIL {label}: write/DDL keyword rejected")
    if ";" in s:
        die(f"CENSUS_RUNNER=FAIL {label}: single statement only")
    return s


def run_value(sql, label):
    """Run a read-only query expected to return a single value; return int."""
    s = assert_readonly(sql, label)
    r = subprocess.run(
        ["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", s],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        die(f"CENSUS_RUNNER=FAIL {label}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'psql failed'}")
    out = r.stdout.strip().splitlines()
    if len(out) != 1:
        die(f"CENSUS_RUNNER=FAIL {label}: expected a single-row single-value result, got {len(out)} rows")
    try:
        return int(out[0].split("|")[0])
    except ValueError:
        die(f"CENSUS_RUNNER=FAIL {label}: non-integer result {out[0]!r}")


def run_pretty(sql, label):
    """Run a read-only query and return psql's aligned human-readable output."""
    s = assert_readonly(sql, label)
    r = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-c", s],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        die(f"CENSUS_RUNNER=FAIL {label}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'psql failed'}")
    return r.stdout


def load(ad, name):
    return json.load(open(os.path.join(ad, name), encoding="utf-8"))


def pct_diff(measured, reference):
    if reference == 0:
        return math.inf if measured else 0.0
    return abs(measured - reference) / reference * 100.0


def cmd_census(plan, ad, limits):
    claim = plan["claim"]
    lines = []
    measured = run_value(plan["census"]["count_sql"], "census count_sql")
    lines.append(f"population count (count_sql): {measured}")
    lines.append("")
    lines.append("== distribution ==")
    lines.append(run_pretty(plan["census"]["distribution_sql"], "census distribution_sql"))
    lines.append("== largest case ==")
    lines.append(run_pretty(plan["census"]["largest_sql"], "census largest_sql"))

    tables = {}
    for t in plan["target"]["tables"]:
        if not IDENT_RE.match(t):
            die(f"CENSUS_RUNNER=FAIL target table {t!r} is not a simple identifier")
        tables[t] = run_value(f'SELECT count(*) FROM "{t}"', f"total of {t}")
        lines.append(f"table total: {t} = {tables[t]}")

    ncq = plan["verification"]["negative_control_query"]
    baseline = run_value(ncq, "negative_control_query")
    lines.append(f"negative-control baseline (rows OUTSIDE the target set): {baseline}")

    # Claim reconciliation: expected_count or expected_range [lo, hi].
    tol = limits["claim_divergence_pct"]
    if claim.get("expected_count") is not None:
        claimed = int(claim["expected_count"])
        diverged = pct_diff(measured, claimed) > tol
        claimed_repr = str(claimed)
    else:
        lo, hi = claim["expected_range"]
        diverged = not (lo * (1 - tol / 100.0) <= measured <= hi * (1 + tol / 100.0))
        claimed_repr = f"[{lo},{hi}]"

    fraction = max((measured / tables[t]) for t in tables if tables[t]) if tables else 0.0
    census = {
        "measured": measured,
        "claimed": claimed_repr,
        "divergence_exceeded": diverged,
        "tables": tables,
        "max_fraction_of_table": round(fraction, 6),
        "negcontrol_baseline": baseline,
    }
    json.dump(census, open(os.path.join(ad, "census.json"), "w", encoding="utf-8"), indent=2)
    open(os.path.join(ad, "census.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")

    if diverged:
        die(f"CLAIM_DIVERGED measured={measured} claimed={claimed_repr} tolerance={tol}% "
            f"(the spec's population claim does not survive measurement - fix the claim in "
            f"backfill-plan.json, or the spec is wrong; the edit is the approval, then resume)", 4)
    if fraction > limits["absolute_max_fraction"]:
        ack = os.path.join(ad, "extraordinary-ack.txt")
        if os.path.isfile(ack) and open(ack, encoding="utf-8").read().strip():
            print(f"EXTRAORDINARY_ACK accepted: {open(ack, encoding='utf-8').read().strip()}")
        else:
            die(f"EXTRAORDINARY_CLAIM fraction={fraction:.4f} limit={limits['absolute_max_fraction']} "
                f"(this backfill touches more than {limits['absolute_max_fraction']:.0%} of a target table - "
                f"systems are rarely that broken in one specific way; usually the DEFINITION is wrong. "
                f"A human writes <artifacts>/extraordinary-ack.txt with a one-line reason to proceed, then resumes)", 5)
    print(f"CENSUS=OK measured={measured} claimed={claimed_repr} fraction={fraction:.4f} negcontrol_baseline={baseline}")


def cmd_sample(plan, ad):
    out = ["== 15 raw matched rows (rows_sql) =="]
    out.append(run_pretty(plan["sample"]["rows_sql"], "sample rows_sql"))
    out.append("== 3 largest per-entity clusters (clusters_sql) ==")
    out.append(run_pretty(plan["sample"]["clusters_sql"], "sample clusters_sql"))
    open(os.path.join(ad, "sample-rows.txt"), "w", encoding="utf-8").write("\n".join(out) + "\n")
    print("SAMPLE=OK sample-rows.txt written (raw rows, never aggregates)")


def cmd_dryrun(plan, ad):
    if plan["instrument"]["kind"] != "sql":
        die("CENSUS_RUNNER=FAIL dryrun subcommand is for kind=sql only (cli dry-runs execute the instrument)")
    n = run_value(plan["instrument"]["dry_run_sql"], "dry_run_sql")
    open(os.path.join(ad, "dryrun-count.txt"), "w", encoding="utf-8").write(str(n) + "\n")
    print(f"DRYRUN=OK would_touch={n}")


def cmd_dryrun_check(ad, limits):
    census = load(ad, "census.json")
    n = int(open(os.path.join(ad, "dryrun-count.txt"), encoding="utf-8").read().strip())
    tol = limits["dryrun_divergence_pct"]
    d = pct_diff(n, census["measured"])
    if d > tol:
        die(f"DRYRUN_DIVERGED would_touch={n} census={census['measured']} divergence={d:.1f}% tolerance={tol}% "
            f"(the instrument does not agree with the census about the population - never arm a command "
            f"whose own count you cannot reconcile)", 6)
    print(f"DRYRUN_CHECK=OK would_touch={n} census={census['measured']} divergence={d:.1f}%")


def cmd_reconcile(plan, ad, touched):
    census = load(ad, "census.json")
    failures = []
    lines = []
    for q in plan["verification"]["reconcile_queries"]:
        v = run_value(q["sql"], f"reconcile {q['id']}")
        exp = q["expect"]
        op, ref = exp["op"], exp["value"]
        ok = {"eq": v == ref, "le": v <= ref, "ge": v >= ref}.get(op)
        if ok is None:
            die(f"CENSUS_RUNNER=FAIL reconcile {q['id']}: expect.op out of enum (eq|le|ge)")
        lines.append(f"reconcile {q['id']}: value={v} expect {op} {ref} -> {'OK' if ok else 'FAIL'} :: {q['question']}")
        if not ok:
            failures.append(f"{q['id']} value={v} expect {op} {ref}")

    after = run_value(plan["verification"]["negative_control_query"], "negative_control_query (post)")
    baseline = census["negcontrol_baseline"]
    lines.append(f"negative control: baseline={baseline} after={after} -> {'OK' if after == baseline else 'FAIL'}")
    if after != baseline:
        failures.append(f"negative-control changed: baseline={baseline} after={after} (the apply touched rows OUTSIDE its set)")

    # Summaries must reconcile with each other: when the apply recorded
    # per-disposition counts, their sum must equal the touched total.
    ar_path = os.path.join(ad, "apply-result.json")
    if os.path.isfile(ar_path):
        ar = json.load(open(ar_path, encoding="utf-8"))
        disp = ar.get("dispositions") or {}
        if disp:
            s = sum(int(v) for v in disp.values())
            lines.append(f"dispositions sum={s} touched={touched} -> {'OK' if s == touched else 'FAIL'}")
            if s != touched:
                failures.append(f"dispositions sum {s} != touched {touched} (if your own columns don't add up, the apply is broken, not the data)")

    open(os.path.join(ad, "reconcile.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    for ln in lines:
        print(ln)
    if failures:
        die("RECONCILE_FAIL " + " ; ".join(failures), 7)
    print(f"RECONCILE=OK queries={len(plan['verification']['reconcile_queries'])} negcontrol=unchanged touched={touched}")


def main():
    if len(sys.argv) < 4:
        die("usage: census-runner.py <subcommand> <plan.json> <artifacts_dir> [args]")
    sub, plan_path, ad = sys.argv[1], sys.argv[2], sys.argv[3]
    plan = json.load(open(plan_path, encoding="utf-8"))
    limits = load(ad, "backfill-limits.json")
    if sub == "census":
        cmd_census(plan, ad, limits)
    elif sub == "sample":
        cmd_sample(plan, ad)
    elif sub == "dryrun":
        cmd_dryrun(plan, ad)
    elif sub == "dryrun-check":
        cmd_dryrun_check(ad, limits)
    elif sub == "reconcile":
        if len(sys.argv) < 5:
            die("usage: census-runner.py reconcile <plan.json> <artifacts_dir> <touched>")
        cmd_reconcile(plan, ad, int(sys.argv[4]))
    else:
        die(f"CENSUS_RUNNER=FAIL unknown subcommand {sub}")


if __name__ == "__main__":
    main()
