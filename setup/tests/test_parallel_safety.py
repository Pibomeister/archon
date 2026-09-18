#!/usr/bin/env python3
"""Concurrency invariants for the lane YAMLs, asserted as a CLASS.

Three runs at once used to be impossible for reasons that had nothing to do
with archon: the lanes hardcoded their smoke ports, swept those ports by number
regardless of who owned the listener, named one constant smoke worktree, and
gated on a count of files in a host-global knowledge base. Every one of those is
a pattern a future lane can reintroduce by copy-paste, which is why this file
tests the pattern and not the instance.

Each invariant carries a negative control: the mutation that should trip it is
applied to a copy and the same check is asserted to FAIL. A test that cannot
fail is not a test -- and here the mutation is also the proof that the anchor
still matches the shipped text.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = ARCHON / "workflows"
SETUP = ARCHON / "setup"

# Lanes with a smoke stack. Value = the port bases that lane is allowed to name.
LANE_BASES = {
    "full-sdlc-api.yaml": {"4123"},
    "full-sdlc-api-lite.yaml": {"4125"},
    "bugfix.yaml": {"4124", "3124"},
    "bugfix-lite.yaml": {"4126", "3126"},
    "full-sdlc-web.yaml": {"4127", "3127"},
}
CODEX_TWINS = {n.replace(".yaml", "-codex.yaml") for n in LANE_BASES}
GROK_TWINS = {n.replace(".yaml", "-grok.yaml") for n in LANE_BASES}
SHIPPED_LANES = list(LANE_BASES) + sorted(CODEX_TWINS | GROK_TWINS)


def twin_parent(name):
    if name.endswith("-codex.yaml"):
        return LANE_BASES[name.replace("-codex.yaml", ".yaml")]
    if name.endswith("-grok.yaml"):
        return LANE_BASES[name.replace("-grok.yaml", ".yaml")]
    raise KeyError(name)

# Anything in these ranges is a smoke port; 4128+/3128+ are the per-run
# allocations and must never appear as a literal at all.
PORT_RE = re.compile(r"\b(?:3|4)1[0-9]{2}\b")


def iter_nodes(nodes, prefix=""):
    for n in nodes:
        yield prefix + n["id"], n
        if "loop_group" in n:
            yield from iter_nodes(n["loop_group"]["nodes"], prefix + n["id"] + ".")


def lane_bodies(path):
    """(node id, bash body) for every node in a lane, loops included."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for nid, node in iter_nodes(doc["nodes"]):
        body = node.get("bash")
        if body:
            yield nid, body


def offending_port_lines(path, allowed):
    """Lines naming a smoke port outside the two sanctioned sites.

    Sanctioned: the resolve-params call that passes the lane's BASES (the one
    literal site derive-lite.py's port map needs), and the port_pids tool-presence
    probe. Everything else must go through $APIPORT / $WEBPORT.
    """
    out = []
    for nid, body in lane_bodies(path):
        for line in body.splitlines():
            hits = set(PORT_RE.findall(line))
            if not hits:
                continue
            sanctioned = (
                ("resolve-params.sh" in line or "resolve-web-params.sh" in line
                 or "profile-preflight.sh" in line
                 or re.search(r'"\$ARTIFACTS_DIR"\s+\d', line))
                or re.match(r"\s*port_pids \d+ >/dev/null", line)
                or line.lstrip().startswith('echo "PREFLIGHT_PORTS')
            )
            if sanctioned and hits <= allowed:
                continue
            if line.lstrip().startswith("#"):
                continue
            out.append((nid, line.strip()))
    return out


class NoLaneHardcodesASmokePort(unittest.TestCase):
    def test_shipped_lanes_are_clean(self):
        for name, allowed in LANE_BASES.items():
            with self.subTest(lane=name):
                self.assertEqual([], offending_port_lines(WORKFLOWS / name, allowed))

    def test_provider_twins_are_clean(self):
        for name in sorted(CODEX_TWINS | GROK_TWINS):
            path = WORKFLOWS / name
            if not path.exists():
                continue
            parent = twin_parent(name)
            with self.subTest(lane=name):
                self.assertEqual([], offending_port_lines(path, parent))

    def test_negative_control_a_reintroduced_literal_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            hurt = Path(td) / "bugfix.yaml"
            text = (WORKFLOWS / "bugfix.yaml").read_text(encoding="utf-8")
            mutated = text.replace(
                '''CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$APIPORT/api-docs-json" || echo 000)''',
                '''CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:4124/api-docs-json || echo 000)''',
                1,
            )
            self.assertNotEqual(text, mutated, "mutation anchor no longer matches the shipped lane")
            hurt.write_text(mutated, encoding="utf-8")
            self.assertNotEqual([], offending_port_lines(hurt, LANE_BASES["bugfix.yaml"]))


class NoLaneSweepsAPortItDoesNotOwn(unittest.TestCase):
    """A kill driven by a port LITERAL reaches whoever is listening on it."""

    SWEEP = re.compile(r"port_pids\s+\"?(?:3|4)1[0-9]{2}\"?\s*(?:\)|\||$)")

    def offenders(self, path):
        bad = []
        for nid, body in lane_bodies(path):
            for line in body.splitlines():
                if "kill" not in line and "PIDS=" not in line and "P=$(" not in line:
                    continue
                if self.SWEEP.search(line):
                    bad.append((nid, line.strip()))
            # `for P in 3124 4124; do ... kill` shape
            for line in body.splitlines():
                if re.match(r"\s*for \w+ in (?:\"?(?:3|4)1[0-9]{2}\"?\s*)+;", line):
                    bad.append((nid, line.strip()))
        return bad

    def test_shipped_lanes_sweep_only_their_own_allocation(self):
        for name in SHIPPED_LANES:
            path = WORKFLOWS / name
            if not path.exists():
                continue
            with self.subTest(lane=name):
                self.assertEqual([], self.offenders(path))

    def test_negative_control_a_literal_sweep_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            hurt = Path(td) / "bugfix.yaml"
            text = (WORKFLOWS / "bugfix.yaml").read_text(encoding="utf-8")
            mutated = text.replace(
                '      for P in "$WEBPORT" "$APIPORT"; do',
                "      for P in 3124 4124; do",
                1,
            )
            self.assertNotEqual(text, mutated, "mutation anchor no longer matches the shipped lane")
            hurt.write_text(mutated, encoding="utf-8")
            self.assertNotEqual([], self.offenders(hurt))


class NoLaneNamesAConstantSmokeWorktree(unittest.TestCase):
    """A fixed worktree name means run B adopts run A's directory, overwrites its
    spec, and rm -rf's it out from under A at teardown."""

    BAD = re.compile(r"\.worktrees/[A-Za-z0-9_-]+(?=[\"'\s])")

    def offenders(self, path):
        bad = []
        for nid, body in lane_bodies(path):
            for line in body.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                for m in self.BAD.finditer(line):
                    frag = m.group(0)
                    # A name is fine when a run-scoped variable is spliced into it.
                    tail = line[m.end():m.end() + 2]
                    if tail.startswith("$") or "$" in frag:
                        continue
                    bad.append((nid, frag, line.strip()))
        return bad

    def test_shipped_lanes_scope_the_smoke_worktree_to_the_run(self):
        for name in SHIPPED_LANES:
            path = WORKFLOWS / name
            if not path.exists():
                continue
            with self.subTest(lane=name):
                self.assertEqual([], self.offenders(path))

    def test_negative_control_a_constant_name_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            hurt = Path(td) / "bugfix.yaml"
            text = (WORKFLOWS / "bugfix.yaml").read_text(encoding="utf-8")
            mutated = text.replace('.worktrees/bugfix-smoke-$SLUG', '.worktrees/bugfix-smoke')
            self.assertNotEqual(text, mutated, "mutation anchor no longer matches the shipped lane")
            hurt.write_text(mutated, encoding="utf-8")
            self.assertNotEqual([], self.offenders(hurt))


class NoLaneGatesOnAHostGlobalFileCount(unittest.TestCase):
    """goodword-kb is shared by every lane. Asserting "exactly one new file"
    fails a run because a DIFFERENT run's capture landed in the window."""

    def offenders(self, path):
        bad = []
        for nid, body in lane_bodies(path):
            if "wiki/change-history/" not in body:
                continue
            for line in body.splitlines():
                if re.search(r'test\s+"\$COUNT"\s*=\s*"1"', line):
                    bad.append((nid, line.strip()))
                if re.search(r'grep -c "\^\?\? wiki/change-history/"', line) and "COUNT=" in line:
                    bad.append((nid, line.strip()))
        return bad

    def test_shipped_lanes_identify_their_own_capture(self):
        for name in SHIPPED_LANES:
            path = WORKFLOWS / name
            if not path.exists():
                continue
            with self.subTest(lane=name):
                self.assertEqual([], self.offenders(path))

    def test_capture_gate_matches_on_the_run_id(self):
        for name in ("full-sdlc-api.yaml", "full-sdlc-api-lite.yaml"):
            body = dict(lane_bodies(WORKFLOWS / name)).get("kb-capture-gate", "")
            with self.subTest(lane=name):
                self.assertIn('RID="$(basename "$ARTIFACTS_DIR")"', body)
                self.assertIn('grep -q "$RID"', body)

    def test_recon_gate_ignores_a_foreign_capture(self):
        for name in ("bugfix.yaml", "full-sdlc-api.yaml", "full-sdlc-api-lite.yaml"):
            body = dict(lane_bodies(WORKFLOWS / name)).get("kb-recon-gate", "")
            with self.subTest(lane=name):
                self.assertIn("grep -v '^?? wiki/change-history/'", body)
                self.assertNotIn(
                    'cmp -s "$ARTIFACTS_DIR/kb-pre-porcelain.txt" "$ARTIFACTS_DIR/kb-now.txt"', body)

    def test_negative_control_a_count_gate_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            hurt = Path(td) / "full-sdlc-api.yaml"
            text = (WORKFLOWS / "full-sdlc-api.yaml").read_text(encoding="utf-8")
            mutated = text.replace(
                '      RID="$(basename "$ARTIFACTS_DIR")"',
                '''      COUNT=$(printf '%s\\n' "$NEW" | grep -c "^?? wiki/change-history/" || true)
      test "$COUNT" = "1" || { echo "KB_CAPTURE_GATE=FAIL"; exit 1; }
      RID="$(basename "$ARTIFACTS_DIR")"''',
                1,
            )
            self.assertNotEqual(text, mutated, "mutation anchor no longer matches the shipped lane")
            hurt.write_text(mutated, encoding="utf-8")
            self.assertNotEqual([], self.offenders(hurt))


class EveryPortConsumerResolvesParamsFirst(unittest.TestCase):
    """$APIPORT/$WEBPORT come from params-env.sh. A node that uses one without
    eval'ing it binds the empty string -- and under `set -u` may not even say so."""

    def offenders(self, path):
        bad = []
        for nid, body in lane_bodies(path):
            use = min((body.index(v) for v in ("$APIPORT", "$WEBPORT") if v in body), default=None)
            if use is None:
                continue
            src = body.find("params-env.sh")
            if src == -1 or src > use:
                bad.append(nid)
        return bad

    def test_shipped_lanes(self):
        for name in SHIPPED_LANES:
            path = WORKFLOWS / name
            if not path.exists():
                continue
            with self.subTest(lane=name):
                self.assertEqual([], self.offenders(path))

    def test_negative_control_a_missing_eval_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            hurt = Path(td) / "bugfix.yaml"
            text = (WORKFLOWS / "bugfix.yaml").read_text(encoding="utf-8")
            mutated = text.replace(
                '      eval "$(bash "$ARCHON_LAYER/setup/params-env.sh" "$AD/params.json")"\n'
                '      WEB_DIR=$(cat "$AD/smoke-stack/web-dir.txt")',
                '      WEB_DIR=$(cat "$AD/smoke-stack/web-dir.txt")',
                1,
            )
            self.assertNotEqual(text, mutated, "mutation anchor no longer matches the shipped lane")
            hurt.write_text(mutated, encoding="utf-8")
            self.assertIn("smoke-auto", self.offenders(hurt))


class EveryTypedGateLeavesItsReasonOnDisk(unittest.TestCase):
    """A failed bash gate's typed line must survive the run.

    archon stores the node's SCRIPT in `node_failed.data`, never its stdout, and
    `archon workflow get` echoes that source and truncates. With one run you read
    the terminal; with three detached runs there is no terminal, and on 2026-09-07
    BOTH gate failures had to be diagnosed by replaying the gate's helper against
    a copy of the artifacts dir. That is a real operating cost of concurrency, so
    the reason goes to disk at the moment it is printed.
    """

    LANES = ["bugfix.yaml", "full-sdlc-api.yaml", "full-sdlc-web.yaml", "backfill.yaml"]
    TYPED = re.compile(r"=(FAIL|PASS)\b")

    def offenders(self, path):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        bad = []
        for nid, node in iter_nodes(doc["nodes"]):
            body = node.get("bash")
            if not body or not self.TYPED.search(body):
                continue
            if "exec > >(tee" not in body:
                bad.append(nid)
        return bad

    def test_every_typed_bash_node_tees(self):
        for lane in self.LANES:
            path = WORKFLOWS / lane
            if not path.exists():
                continue
            with self.subTest(lane=lane):
                self.assertEqual([], self.offenders(path))

    def test_the_tee_is_guarded_with_if_not_and(self):
        # `[ -n "$X" ] && exec ...` would make a false test the script's last
        # status, and under `set -e` that kills the node before it does anything.
        for lane in self.LANES:
            path = WORKFLOWS / lane
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(lane=lane):
                self.assertNotRegex(text, r'\]\s*&&\s*exec > >\(tee')
                # stderr must stay on stderr. `2>&1` would merge it into stdout,
                # and archon persists bash STDERR but not stdout (the comment in
                # rca-converge says so, and review-loop's converge was written
                # because of it) -- merging would have made every gate's output
                # invisible to archon while "improving" observability.
                self.assertNotRegex(text, r'exec > >\(tee -a "\$ARTIFACTS_DIR/node-[^"]+"\) 2>&1')

    def test_the_tee_preserves_stdout_and_exit_code(self):
        # Behavioural, not textual: the caller must still see the output (archon
        # captures it) and the gate's verdict must still be its exit code.
        doc = yaml.safe_load((WORKFLOWS / "bugfix.yaml").read_text(encoding="utf-8"))
        gate = dict(iter_nodes(doc["nodes"]))["capability-gate"]["bash"]
        with tempfile.TemporaryDirectory() as d:
            Path(d, "bug-report.md").write_text(
                "## Intake gaps\n\n- Affected account id unknown - retrievable_by: probe-prod\n"
                "\n## Classification\n\n`defect`\n", encoding="utf-8")
            Path(d, "capabilities.json").write_text(json.dumps(
                {"schema_version": 2,
                 "capabilities": {"prod-db": {"status": "UNAVAILABLE", "reason": "aws-session-expired x"}}}),
                encoding="utf-8")
            r = subprocess.run(["bash", "-c", gate], capture_output=True, encoding="utf-8",
                               env={**os.environ, "ARTIFACTS_DIR": d})
            self.assertEqual(1, r.returncode, r.stdout)
            self.assertIn("CAPABILITY_GATE=FAIL", r.stdout)
            written = Path(d, "node-capability-gate.out")
            self.assertTrue(written.is_file(), "the gate's reason did not reach disk")
            self.assertIn("CAPABILITY_GATE=FAIL", written.read_text(encoding="utf-8"))

    def test_negative_control_a_node_without_the_tee_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            hurt = Path(td) / "bugfix.yaml"
            text = (WORKFLOWS / "bugfix.yaml").read_text(encoding="utf-8")
            mutated = text.replace(
                'if [ -n "${ARTIFACTS_DIR-}" ]; then exec > >(tee -a "$ARTIFACTS_DIR/node-rca-gate.out") '
                '2> >(tee -a "$ARTIFACTS_DIR/node-rca-gate.out" >&2); fi\n',
                "", 1)
            self.assertNotEqual(text, mutated, "mutation anchor no longer matches the shipped lane")
            hurt.write_text(mutated, encoding="utf-8")
            self.assertIn("rca-gate", self.offenders(hurt))


class PortAllocIsDisjointByLane(unittest.TestCase):
    SCRIPT = SETUP / "port-alloc.sh"

    def alloc(self, base, slug):
        r = subprocess.run(["bash", str(self.SCRIPT), str(base), slug],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(0, r.returncode, r.stderr)
        return int(r.stdout.strip())

    def test_lane_bases_cannot_collide(self):
        """Stride 10 with bases inside one decade is what makes cross-lane
        collision impossible -- not luck about how slugs hash."""
        bases = sorted({int(b) for bs in LANE_BASES.values() for b in bs})
        # api bases live in the 4100s, web bases in the 3100s -- two families
        # whose allocation ranges (base .. base+190) cannot reach each other.
        # Within a family the residue mod 10 is what keeps lanes apart.
        for family in (3, 4):
            fam = [b for b in bases if b // 1000 == family]
            residues = [b % 10 for b in fam]
            self.assertEqual(len(residues), len(set(residues)),
                             f"lane bases share a residue mod 10 within {family}xxx: {fam}")
        api = [b for b in bases if b // 1000 == 4]
        web = [b for b in bases if b // 1000 == 3]
        self.assertLess(max(web) + 190, min(api), "web and api allocation ranges overlap")

    def test_allocation_is_deterministic(self):
        a = self.alloc(4124, "eng-3059-paused-trialing")
        b = self.alloc(4124, "eng-3059-paused-trialing")
        self.assertEqual(a, b)

    def test_allocation_stays_on_the_lane_stride(self):
        for slug in ("eng-3059", "eng-3842", "some-other-report", "a", ""):
            if not slug:
                continue
            self.assertEqual(4, self.alloc(4124, slug) % 10)
            self.assertEqual(3, self.alloc(4123, slug) % 10)

    def test_different_reports_usually_get_different_ports(self):
        slugs = [f"eng-{n}" for n in range(3000, 3040)]
        ports = {self.alloc(4124, s) for s in slugs}
        self.assertGreater(len(ports), 10, "hash is not spreading across slots")

    def test_negative_control_a_stride_of_one_collides_across_lanes(self):
        """Revert the stride and the disjointness proof fails."""
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("PORT=$(( BASE + 10 * SLOT ))", text)
        with tempfile.TemporaryDirectory() as td:
            hurt = Path(td) / "port-alloc.sh"
            hurt.write_text(text.replace("PORT=$(( BASE + 10 * SLOT ))",
                                         "PORT=$(( BASE + SLOT ))"), encoding="utf-8")
            def alloc(base, slug):
                r = subprocess.run(["bash", str(hurt), str(base), slug],
                                   capture_output=True, encoding="utf-8")
                return int(r.stdout.strip())
            # With stride 1, an api-lane slug and a bugfix-lane slug can land on
            # the same port. Show at least one such pair exists.
            api = {alloc(4123, f"s{i}") for i in range(20)}
            bug = {alloc(4124, f"s{i}") for i in range(20)}
            self.assertTrue(api & bug, "stride-1 mutation should produce a cross-lane collision")


class E2eMutexSerializesTheSharedStack(unittest.TestCase):
    SCRIPT = SETUP / "e2e-mutex.sh"

    def run_op(self, lock, op, owner):
        env = dict(os.environ, ARCHON_E2E_LOCK=str(lock))
        return subprocess.run(["bash", str(self.SCRIPT), op, owner],
                              capture_output=True, encoding="utf-8", env=env)

    def test_second_owner_is_refused_and_told_who_holds_it(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "e2e.lock"
            self.assertEqual(0, self.run_op(lock, "acquire", "/ad/A").returncode)
            r = self.run_op(lock, "acquire", "/ad/B")
            self.assertEqual(1, r.returncode)
            self.assertIn("/ad/A", r.stdout)
            self.assertIn("E2E_MUTEX=FAIL", r.stdout)

    def test_acquire_is_reentrant_for_the_owner(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "e2e.lock"
            self.run_op(lock, "acquire", "/ad/A")
            r = self.run_op(lock, "acquire", "/ad/A")
            self.assertEqual(0, r.returncode)
            self.assertIn("E2E_MUTEX=HELD", r.stdout)

    def test_a_stranger_cannot_release_it(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "e2e.lock"
            self.run_op(lock, "acquire", "/ad/A")
            self.run_op(lock, "release", "/ad/B")
            self.assertTrue(lock.exists(), "a non-owner released the lock")
            self.run_op(lock, "release", "/ad/A")
            self.assertFalse(lock.exists())

    def test_both_compose_boot_sites_take_it(self):
        bodies = dict(lane_bodies(WORKFLOWS / "bugfix.yaml"))
        for nid in ("exit-gate", "smoke-stack"):
            with self.subTest(node=nid):
                self.assertIn("docker compose -f local-env-compose.e2e.yaml up", bodies[nid])
                self.assertRegex(bodies[nid], r'e2e-mutex\.sh"?\s+acquire')
        self.assertRegex(bodies["smoke-teardown"], r'e2e-mutex\.sh"?\s+release')

    def test_negative_control_dropping_the_acquire_is_caught(self):
        bodies = dict(lane_bodies(WORKFLOWS / "bugfix.yaml"))
        stripped = bodies["smoke-stack"].replace(
            'bash "$ARCHON_LAYER/setup/e2e-mutex.sh" acquire "$AD"', "true")
        self.assertNotEqual(bodies["smoke-stack"], stripped, "mutation anchor no longer matches")
        self.assertNotRegex(stripped, r'e2e-mutex\.sh"?\s+acquire')


class ParamsCarryTheRunsPorts(unittest.TestCase):
    def test_resolve_params_writes_them_and_params_env_exports_them(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            root = td / "root"
            (root / ".archon" / "setup").mkdir(parents=True)
            for s in ("port-alloc.sh", "resolve-params.sh", "params-env.sh",
                      "repo-profile.sh"):
                shutil.copy(SETUP / s, root / ".archon" / "setup" / s)
            spec = td / "ENG-9999-a-report.md"
            spec.write_text("# report\n", encoding="utf-8")
            ad = td / "ad"
            ad.mkdir()
            r = subprocess.run(
                ["bash", str(root / ".archon/setup/resolve-params.sh"),
                 str(root), str(spec), str(ad), "4124", "3124"],
                capture_output=True, encoding="utf-8")
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            params = json.loads((ad / "params.json").read_text(encoding="utf-8"))
            self.assertEqual(4, params["api_port"] % 10)
            self.assertEqual(4, params["web_port"] % 10)

            env = subprocess.run(
                ["bash", str(root / ".archon/setup/params-env.sh"), str(ad / "params.json")],
                capture_output=True, encoding="utf-8")
            self.assertIn(f"APIPORT={params['api_port']}", env.stdout)
            self.assertIn(f"WEBPORT={params['web_port']}", env.stdout)

    def test_a_lane_without_bases_gets_no_ports(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            root = td / "root"
            (root / ".archon" / "setup").mkdir(parents=True)
            for s in ("port-alloc.sh", "resolve-params.sh", "params-env.sh",
                      "repo-profile.sh"):
                shutil.copy(SETUP / s, root / ".archon" / "setup" / s)
            spec = td / "x.md"
            spec.write_text("x\n", encoding="utf-8")
            ad = td / "ad"
            ad.mkdir()
            subprocess.run(["bash", str(root / ".archon/setup/resolve-params.sh"),
                            str(root), str(spec), str(ad)],
                           capture_output=True, encoding="utf-8", check=True)
            params = json.loads((ad / "params.json").read_text(encoding="utf-8"))
            self.assertNotIn("api_port", params)
            env = subprocess.run(["bash", str(root / ".archon/setup/params-env.sh"),
                                  str(ad / "params.json")],
                                 capture_output=True, encoding="utf-8")
            evaluated = subprocess.run(
                ["bash", "-c", 'eval "$1"; test -z "$APIPORT" && test -z "$WEBPORT"',
                 "params-check", env.stdout], capture_output=True, text=True,
            )
            self.assertEqual(evaluated.returncode, 0, evaluated.stderr)


if __name__ == "__main__":
    unittest.main()
