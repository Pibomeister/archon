#!/usr/bin/env bash
# Armed execution for the backfill lane: the ONLY code path that runs the
# instrument's apply form against prod. Everything here is enforcement:
#
#   - Kill switch: the file $AD/KILL_SWITCH refuses execution, typed, checked
#     at start AND between chunks. The arm node proves this refusal fires
#     (engage -> expect exit 3 -> disengage) BEFORE the first real apply.
#   - Hash pinning: the command executed is byte-identical to what the human
#     approved. armed-command.txt is re-hashed against armed-command.sha256
#     AND the hash must appear verbatim in the approved packet html.
#   - Bounds: max_rows/chunk_size/statement_timeout come from bounds.json
#     (already the min of spec proposal, census*1.1, absolute ceiling).
#     Cumulative touched > max_rows -> engage the kill switch, typed
#     BOUND_BREACH, exit 8. These bounds hold regardless of expected volume.
#   - Stall: no progress for stall_seconds (cli) or chunks exhausted without
#     the population draining (sql) -> typed APPLY_STALLED, exit 9.
#
# Usage: armed-exec.sh <artifacts_dir> apply
# Exit codes: 0 ok, 3 kill switch, 8 bound breach, 9 stalled, 10 exec failure.
# The caller exports connection env (PG* and/or the plan's env_map) BEFORE
# invoking; this script never reads Secrets Manager and never logs credentials.
set -uo pipefail

AD="${1:?usage: armed-exec.sh <artifacts_dir> apply}"
MODE="${2:?usage: armed-exec.sh <artifacts_dir> apply}"
test "$MODE" = apply || { echo "ARMED_EXEC=FAIL unknown mode $MODE"; exit 10; }

# --- Kill switch: first check, before anything else runs.
if [ -f "$AD/KILL_SWITCH" ]; then
  echo "KILL_SWITCH=ENGAGED refusing to execute (remove $AD/KILL_SWITCH only as a deliberate human act)"
  exit 3
fi

test -s "$AD/armed-command.txt" || { echo "ARMED_EXEC=FAIL no armed-command.txt"; exit 10; }
test -s "$AD/armed-command.sha256" || { echo "ARMED_EXEC=FAIL no armed-command.sha256"; exit 10; }
test -s "$AD/bounds.json" || { echo "ARMED_EXEC=FAIL no bounds.json"; exit 10; }

# --- Hash pinning (belt inside the executor; the apply node checks too).
if command -v sha256sum >/dev/null 2>&1; then
  HAVE=$(sha256sum "$AD/armed-command.txt" | cut -d' ' -f1)
else
  HAVE=$(shasum -a 256 "$AD/armed-command.txt" | cut -d' ' -f1)
fi
WANT=$(cat "$AD/armed-command.sha256")
test "$HAVE" = "$WANT" || { echo "ARMED_DRIFT armed-command.txt hash $HAVE != approved $WANT (the command changed after arming - never execute it; re-run arm and re-approve)"; exit 10; }

export AD
python3 - <<'PY'
import json, os, re, select, signal, subprocess, sys, time

ad = os.environ["AD"]
plan = json.load(open(os.path.join(ad, "backfill-plan.json"), encoding="utf-8"))
bounds = json.load(open(os.path.join(ad, "bounds.json"), encoding="utf-8"))
cmd = open(os.path.join(ad, "armed-command.txt"), encoding="utf-8").read()
kind = plan["instrument"]["kind"]
max_rows = int(bounds["max_rows"])
chunk_size = int(bounds["chunk_size"])
stall_seconds = int(bounds["stall_seconds"])
stmt_ms = int(bounds["statement_timeout_ms"])
log = open(os.path.join(ad, "apply.log"), "a", encoding="utf-8")

def out(line):
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()

def kill_switch(reason):
    open(os.path.join(ad, "KILL_SWITCH"), "w", encoding="utf-8").write(reason + "\n")

def switch_engaged():
    return os.path.isfile(os.path.join(ad, "KILL_SWITCH"))

dispositions = {}
DISP_RE = re.compile(r"DISPOSITION:([A-Za-z0-9_-]+)=(\d+)")

def finish(touched, chunks, status):
    json.dump({"touched": touched, "chunks": chunks, "status": status,
               "dispositions": dispositions},
              open(os.path.join(ad, "apply-result.json"), "w", encoding="utf-8"), indent=2)

TAG_RE = re.compile(r"^(?:UPDATE|DELETE|MERGE)\s+(\d+)$|^INSERT\s+\d+\s+(\d+)$")

if kind == "sql":
    # Chunk loop: the armed SQL is one bounded statement that drains the
    # population ({CHUNK_SIZE} already substituted). Repeat until the command
    # tag reports 0 rows. Absolute backstop: a non-draining statement cannot
    # loop forever - chunk count is capped from max_rows.
    env = dict(os.environ)
    env["PGOPTIONS"] = (env.get("PGOPTIONS", "") + f" -c statement_timeout={stmt_ms}").strip()
    max_chunks = (max_rows + chunk_size - 1) // chunk_size + 5
    touched, chunk = 0, 0
    while True:
        if switch_engaged():
            out("KILL_SWITCH=ENGAGED refusing next chunk")
            finish(touched, chunk, "kill_switch")
            sys.exit(3)
        chunk += 1
        r = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", cmd],
                           capture_output=True, text=True, encoding="utf-8", env=env)
        if r.returncode != 0:
            err = (r.stderr.strip().splitlines() or ["psql failed"])[-1]
            out(f"APPLY_EXEC=FAIL chunk={chunk} {err}")
            finish(touched, chunk, "exec_failure")
            sys.exit(10)
        tag = (r.stdout.strip().splitlines() or [""])[-1].strip()
        m = TAG_RE.match(tag)
        if not m:
            out(f"APPLY_EXEC=FAIL chunk={chunk} unparseable command tag: {tag!r}")
            finish(touched, chunk, "exec_failure")
            sys.exit(10)
        n = int(m.group(1) or m.group(2))
        touched += n
        out(f"CHUNK={chunk} touched={n} total={touched}")
        if touched > max_rows:
            kill_switch(f"BOUND_BREACH total={touched} max_rows={max_rows}")
            out(f"BOUND_BREACH touched={touched} max_rows={max_rows} (kill switch engaged; snapshot + restore-command.txt are the recovery)")
            finish(touched, chunk, "bound_breach")
            sys.exit(8)
        if n == 0:
            finish(touched, chunk, "ok")
            out(f"APPLY=OK touched={touched} chunks={chunk}")
            sys.exit(0)
        if chunk >= max_chunks:
            kill_switch(f"APPLY_STALLED chunks={chunk} without draining")
            out(f"APPLY_STALLED chunks={chunk} touched={touched} (the armed SQL is not draining its population - kill switch engaged)")
            finish(touched, chunk, "stalled")
            sys.exit(9)
else:
    # cli: run the armed command, stream output, enforce bounds + stall from
    # the instrument's typed progress lines. {MAX_ROWS} is already baked into
    # the command itself (intake-gate requires the placeholder), so this
    # monitor is the OUTER bound, not the only one.
    prog = re.compile(plan["instrument"].get("progress_regex") or r"TOUCHED=([0-9]+)")
    cwd = bounds.get("cwd") or None
    p = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=True)
    def killpg():
        # Never raise: the typed stop line and exit code matter more than a
        # perfectly clean kill (group signals can be EPERM under sandboxes).
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            time.sleep(1)
            if p.poll() is not None:
                return
    touched, chunks = 0, 0
    buf = b""
    last = time.time()
    while True:
        if switch_engaged():
            killpg()
            out("KILL_SWITCH=ENGAGED terminated the instrument")
            finish(touched, chunks, "kill_switch")
            sys.exit(3)
        ready, _, _ = select.select([p.stdout], [], [], 5)
        if ready:
            data = os.read(p.stdout.fileno(), 65536)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace")
                last = time.time()
                out(f"CLI| {line}")
                dm = DISP_RE.search(line)
                if dm:
                    dispositions[dm.group(1)] = dispositions.get(dm.group(1), 0) + int(dm.group(2))
                m = prog.search(line)
                if m:
                    chunks += 1
                    touched += int(m.group(1))
                    if touched > max_rows:
                        killpg()
                        kill_switch(f"BOUND_BREACH total={touched} max_rows={max_rows}")
                        out(f"BOUND_BREACH touched={touched} max_rows={max_rows} (instrument terminated; snapshot + restore-command.txt are the recovery)")
                        finish(touched, chunks, "bound_breach")
                        sys.exit(8)
        if time.time() - last > stall_seconds:
            killpg()
            kill_switch(f"APPLY_STALLED no progress for {stall_seconds}s")
            out(f"APPLY_STALLED no progress for {stall_seconds}s (instrument terminated; kill switch engaged)")
            finish(touched, chunks, "stalled")
            sys.exit(9)
        if p.poll() is not None and not ready:
            break
    rc = p.wait()
    if buf:
        out("CLI| " + buf.decode("utf-8", errors="replace"))
    if rc != 0:
        out(f"APPLY_EXEC=FAIL instrument exited rc={rc}")
        finish(touched, chunks, "exec_failure")
        sys.exit(10)
    finish(touched, chunks, "ok")
    out(f"APPLY=OK touched={touched} chunks={chunks}")
    sys.exit(0)
PY
