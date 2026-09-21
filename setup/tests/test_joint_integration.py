#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
import importlib.util


ARCHON = Path(__file__).resolve().parents[2]
RUNNER = ARCHON / "setup/run-joint-integration.py"
FEATURE_BUDGET = ARCHON / "setup/feature-budget.py"
CHAIN = "a" * 32


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(data):
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class JointIntegrationRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="joint integration ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # The runner takes the host e2e mutex; never the machine's real lock.
        self.lock = self.root / "e2e.lock"
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                        ARCHON_E2E_LOCK=str(self.lock), ARCHON_DB=str(self.root / "no-archon.db"))
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.api_commit = self.init_repo("api", "api-candidate\n")
        self.mcp_commit = self.init_repo("goodword-mcp", "mcp-candidate\n")
        self.plan_digest = None

    def init_repo(self, name, contents):
        repo = self.root / name
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], env=self.env, check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], env=self.env, check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.com"], env=self.env, check=True)
        (repo / "candidate.txt").write_text(contents, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "candidate.txt"], env=self.env, check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate"], env=self.env, check=True)
        # The root clone's untracked runtime state, as on a real machine: what
        # candidate_env.py resolves for each repo profile's runtime_deps and
        # runtime_env_files.
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "marker").write_text(name, encoding="utf-8")
        for env_file in (".env", ".env.e2e"):
            (repo / env_file).write_text(f"{name}{env_file}\n", encoding="utf-8")
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], env=self.env, text=True).strip()

    def write_candidates(self, **overrides):
        if self.plan_digest is None:
            raise AssertionError("write_plan must run before write_candidates")
        body = {
            "schema": "archon.joint-candidates.v1",
            "plan_digest": self.plan_digest,
            "approved_plan_digest": self.plan_digest,
            "candidate_heads": {
                "api": self.api_commit,
                "goodword-mcp": self.mcp_commit,
            },
            "repositories": {
                "api": {"source_worktree": str(self.root / "api"), "commit": self.api_commit},
                "goodword-mcp": {"source_worktree": str(self.root / "goodword-mcp"), "commit": self.mcp_commit},
            },
        }
        body.update(overrides)
        write_json(self.artifacts / "candidate-revisions.json", {
            **body,
        })

    def write_plan(self, command, **scenario_overrides):
        scenario = {
            "name": "fixture",
            "uses": ["api", "goodword-mcp"],
            "commands": [command],
            "expected_tests": ["fixture contract"],
        }
        scenario.update(scenario_overrides)
        plan = {
            "schema": "archon.joint-feature-plan.v1",
            "repositories": ["api", "goodword-mcp"],
            "dependency_order": ["api", "goodword-mcp"],
            "stages": {
                "api": {
                    "depends_on": [],
                    "files_allowlist": ["candidate.txt"],
                    "test_patterns": ["candidate"],
                    "verification": ["fixture"],
                },
                "goodword-mcp": {
                    "depends_on": ["api"],
                    "files_allowlist": ["candidate.txt"],
                    "test_patterns": ["candidate"],
                    "verification": ["fixture"],
                },
            },
            "integration": {
                "scenarios": [scenario],
            },
        }
        self.plan_digest = digest(plan)
        write_json(self.artifacts / "joint-plan.json", plan)

    def run_runner(self):
        return subprocess.run(
            ["python3", str(RUNNER), "--artifacts", str(self.artifacts)],
            env=self.env, capture_output=True, text=True)

    def assert_pid_gone(self, pid):
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        self.fail(f"process still alive after joint integration cleanup: {pid}")

    def init_budget(self, control):
        control.mkdir(mode=0o700, exist_ok=True)
        result = subprocess.run([
            "python3", str(FEATURE_BUDGET), "--control-dir", str(control),
            "init", "--chain-id", CHAIN,
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def terminate_registered_groups(self, control, run_id):
        return subprocess.run([
            "python3", str(FEATURE_BUDGET), "--control-dir", str(control),
            "terminate-groups", "--chain-id", CHAIN, "--run-id", run_id, "--json",
        ], capture_output=True, text=True)

    def test_registered_actual_run_shell_command_survives_fingerprint_until_budget_kill(self):
        spec = importlib.util.spec_from_file_location("run_joint_integration", RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        run_id = "cafebabe99"
        control = self.root / "control"
        self.init_budget(control)
        child_file = self.artifacts / "actual-child.pid"
        env = dict(self.env,
                   ARCHON_CONTROL_DIR=str(control),
                   ARCHON_FEATURE_CHAIN_ID=CHAIN,
                   ARCHON_FEATURE_RUN_ID=run_id)
        command = (
            "python3 - <<'PY' &\n"
            "import os, pathlib, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"pathlib.Path({str(child_file)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
            "PY\n"
            "wait"
        )
        holder = {}
        import threading
        thread = threading.Thread(
            target=lambda: holder.update(result=module.run_shell_command(command, self.artifacts, env, 30, "integration:actual:1")),
            daemon=True,
        )
        thread.start()
        deadline = time.time() + 5
        while not child_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(child_file.exists(), "actual run_shell_command child did not start")
        child_pid = int(child_file.read_text(encoding="utf-8"))

        result = self.terminate_registered_groups(control, run_id)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("integration:actual:1", payload["terminated"][0]["label"])
        self.assertNotIn("refused", payload["terminated"][0]["result"])
        self.assert_pid_gone(child_pid)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "run_shell_command did not return after registered group kill")
        output, returncode, timed_out, _pgid, registered = holder["result"]
        self.assertTrue(registered)
        self.assertFalse(timed_out)
        self.assertNotEqual(0, returncode)

    def test_runs_approved_command_against_detached_candidate_worktrees(self):
        self.write_plan(
            "python3 - <<'PY'\n"
            "import os, pathlib\n"
            "api = pathlib.Path(os.environ['ARCHON_REPO_API_WORKTREE']) / 'candidate.txt'\n"
            "mcp = pathlib.Path(os.environ['ARCHON_REPO_GOODWORD_MCP_WORKTREE']) / 'candidate.txt'\n"
            "assert api.read_text() == 'api-candidate\\n'\n"
            "assert mcp.read_text() == 'mcp-candidate\\n'\n"
            "assert os.environ['ARCHON_REPO_API_COMMIT']\n"
            "assert os.environ['ARCHON_REPO_GOODWORD_MCP_COMMIT']\n"
            "pathlib.Path('fixture-evidence.txt').write_text('verified\\n')\n"
            "print('ARCHON_INTEGRATION_TESTS=1')\n"
            "PY"
            ,
            expected_artifacts=["fixture-evidence.txt"],
        )
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_INTEGRATION=PASS", result.stdout)
        report = json.loads((self.artifacts / "joint-integration-result.json").read_text(encoding="utf-8"))
        self.assertEqual("passed", report["status"])
        self.assertEqual(self.plan_digest, report["plan_digest"])
        self.assertEqual({"api": self.api_commit, "goodword-mcp": self.mcp_commit}, report["candidate_revisions"])
        self.assertEqual(1, report["counters"]["expected_tests"])
        self.assertEqual([{"name": "fixture", "status": "passed", "expected_tests": ["fixture contract"], "reported_tests": 1, "commands": [0],
                           "artifacts": [{"bytes": 9, "exists": True, "path": "fixture-evidence.txt",
                                          "sha256": hashlib.sha256(b"verified\n").hexdigest()}]}],
                         report["tests"])
        self.assertEqual(0, report["commands"][0]["returncode"])
        self.assertIsInstance(report["commands"][0]["launcher_pgid"], int)
        self.assertEqual(1, report["commands"][0]["reported_tests"])
        evidence = json.loads((self.artifacts / "integration-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(report, evidence)
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())

    def test_commands_receive_the_chain_api_port_from_params(self):
        params = self.artifacts / "params.json"
        data = json.loads(params.read_text(encoding="utf-8")) if params.exists() else {}
        data["api_port"] = 4999
        params.write_text(json.dumps(data), encoding="utf-8")
        self.write_plan(
            "python3 -c \"import os; assert os.environ['ARCHON_API_PORT'] == '4999'; print('ARCHON_INTEGRATION_TESTS=1')\""
        )
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_INTEGRATION=PASS", result.stdout)

    def test_joint_e2e_script_defaults_to_the_exported_port(self):
        script = (Path(__file__).resolve().parents[1] / "joint-api-mcp-e2e.sh").read_text(encoding="utf-8")
        self.assertIn('PORT="${ARCHON_API_PORT:-4213}"', script)

    def test_joint_e2e_script_splits_port_from_extra_jest_args(self):
        script = Path(__file__).resolve().parents[1] / "joint-api-mcp-e2e.sh"
        head = script.read_text(encoding="utf-8").split('URL="http://localhost:$PORT"')[0]
        probe = head.split("set -euo pipefail", 1)[1] + '\necho "PORT=$PORT PATTERN=$PATTERN EXTRA=${JEST_EXTRA[*]-}"\n'
        env = dict(os.environ, ARCHON_API_PORT="4999", ARCHON_REPO_API_WORKTREE="/a", ARCHON_REPO_GOODWORD_MCP_WORKTREE="/m")
        def run(*args):
            return subprocess.run(["bash", "-c", "set -euo pipefail" + probe, "x", *args], env=env,
                                  capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual("PORT=4999 PATTERN=tests/a.ts EXTRA=-t debrief", run("tests/a.ts", "-t", "debrief"))
        self.assertEqual("PORT=4321 PATTERN=tests/a.ts EXTRA=-t x", run("tests/a.ts", "4321", "-t", "x"))
        self.assertEqual("PORT=4999 PATTERN=tests/a.ts EXTRA=", run("tests/a.ts"))

    def test_candidate_env_failure_is_infrastructure(self):
        script = Path(__file__).resolve().parents[1] / "joint-api-mcp-e2e.sh"
        with tempfile.TemporaryDirectory() as td:
            setup = Path(td) / "setup"
            setup.mkdir()
            shutil.copy(script, setup / "joint-api-mcp-e2e.sh")
            (setup / "candidate_env.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
            api_wt = Path(td) / "api"
            mcp_wt = Path(td) / "mcp"
            api_wt.mkdir()
            mcp_wt.mkdir()
            env = dict(os.environ,
                       ARCHON_REPO_API_WORKTREE=str(api_wt),
                       ARCHON_REPO_GOODWORD_MCP_WORKTREE=str(mcp_wt),
                       ARCHON_API_PORT="59991")
            r = subprocess.run(["bash", str(setup / "joint-api-mcp-e2e.sh"), "tests/x.ts"],
                               capture_output=True, text=True, env=env, cwd=td, timeout=30)
            self.assertNotEqual(0, r.returncode)
            self.assertIn("JOINT_E2E=FAIL class=infrastructure api candidate env", r.stdout)

    def test_joint_boot_dead_process_with_stack_up_is_product(self):
        script = (Path(__file__).resolve().parents[1] / "joint-api-mcp-e2e.sh").read_text(encoding="utf-8")
        start = script.index("stack_down()")
        end = script.index("json_field()")
        boot = script[start:end]
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            (bin_dir / "docker").write_text(
                "#!/bin/bash\nprintf '%s\\n' postgres-db dynamodb-local\n", encoding="utf-8")
            (bin_dir / "docker").chmod(0o755)
            (bin_dir / "curl").write_text("#!/bin/bash\nprintf 000\n", encoding="utf-8")
            (bin_dir / "curl").chmod(0o755)
            (bin_dir / "sleep").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
            (bin_dir / "sleep").chmod(0o755)
            out = Path(td) / "joint-api-mcp-e2e"
            out.mkdir()
            body = (
                "set -euo pipefail\n"
                f"OUT={out}\nURL=http://localhost:1\nSRV=99999999\n"
                + boot
            )
            r = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                               env={**os.environ, "PATH": f"{bin_dir}:/bin:/usr/bin"}, timeout=20)
            self.assertNotEqual(0, r.returncode)
            self.assertIn("JOINT_E2E=FAIL api-boot exited before ready", r.stdout)
            self.assertNotIn("class=infrastructure", r.stdout)

    def test_joint_boot_dead_process_with_stack_down_is_infrastructure(self):
        script = (Path(__file__).resolve().parents[1] / "joint-api-mcp-e2e.sh").read_text(encoding="utf-8")
        start = script.index("stack_down()")
        end = script.index("json_field()")
        boot = script[start:end]
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            (bin_dir / "docker").write_text(
                "#!/bin/bash\nprintf '%s\\n' postgres-db\n", encoding="utf-8")
            (bin_dir / "docker").chmod(0o755)
            (bin_dir / "curl").write_text("#!/bin/bash\nprintf 000\n", encoding="utf-8")
            (bin_dir / "curl").chmod(0o755)
            (bin_dir / "sleep").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
            (bin_dir / "sleep").chmod(0o755)
            out = Path(td) / "joint-api-mcp-e2e"
            out.mkdir()
            body = (
                "set -euo pipefail\n"
                f"OUT={out}\nURL=http://localhost:1\nSRV=99999999\n"
                + boot
            )
            r = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                               env={**os.environ, "PATH": f"{bin_dir}:/bin:/usr/bin"}, timeout=20)
            self.assertNotEqual(0, r.returncode)
            self.assertIn("class=infrastructure", r.stdout)
            self.assertIn("api-boot exited before ready", r.stdout)

    def test_joint_live_non_200_with_stack_up_is_product(self):
        script = (Path(__file__).resolve().parents[1] / "joint-api-mcp-e2e.sh").read_text(encoding="utf-8")
        start = script.index("stack_down()")
        end = script.index("json_field()")
        boot = script[start:end]
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            (bin_dir / "docker").write_text(
                "#!/bin/bash\nprintf '%s\\n' postgres-db dynamodb-local\n", encoding="utf-8")
            (bin_dir / "docker").chmod(0o755)
            (bin_dir / "curl").write_text("#!/bin/bash\nprintf 500\n", encoding="utf-8")
            (bin_dir / "curl").chmod(0o755)
            (bin_dir / "sleep").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
            (bin_dir / "sleep").chmod(0o755)
            out = Path(td) / "joint-api-mcp-e2e"
            out.mkdir()
            body = (
                "set -euo pipefail\n"
                f"OUT={out}\nURL=http://localhost:1\n"
                "SRV=$(/bin/sleep 30 >/dev/null 2>&1 & echo $!)\n"
                + boot
            )
            r = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                               env={**os.environ, "PATH": f"{bin_dir}:/bin:/usr/bin"}, timeout=20)
            self.assertNotEqual(0, r.returncode)
            self.assertIn("JOINT_E2E=FAIL api boot code=500", r.stdout)
            self.assertNotIn("class=infrastructure", r.stdout)

    def test_second_identity_401_is_infrastructure_and_does_not_start_jest(self):
        script = (Path(__file__).resolve().parents[1] / "joint-api-mcp-e2e.sh").read_text(encoding="utf-8")
        block = script[script.index("SECOND_PROBE="):script.index("RC=0")]
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            (bin_dir / "curl").write_text("#!/bin/bash\nprintf 401\n", encoding="utf-8")
            (bin_dir / "curl").chmod(0o755)
            (bin_dir / "pnpm").write_text("#!/bin/bash\necho JEST_STARTED >&2; exit 0\n", encoding="utf-8")
            (bin_dir / "pnpm").chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_dir}:/bin:/usr/bin",
                       OTP_EMAIL_SECOND="edy+archon2@goodword.com")
            body = 'set -euo pipefail\nURL=http://localhost:1\nTOKEN_SECOND=tok\nOTP_EMAIL_SECOND=edy+archon2@goodword.com\n' + block
            r = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env, timeout=20)
            self.assertNotEqual(0, r.returncode)
            self.assertIn("class=infrastructure second-identity-unsubscribed code=401", r.stdout)
            self.assertNotIn("JEST_STARTED", r.stdout + r.stderr)

    def test_runs_structured_command_in_repo_candidate_worktree_with_expanded_refs(self):
        self.write_plan({
            "repo": "api",
            "argv": [
                "python3",
                "-c",
                (
                    "import pathlib, sys\n"
                    "cwd = pathlib.Path.cwd()\n"
                    "assert cwd == pathlib.Path(sys.argv[1])\n"
                    "assert (cwd / 'candidate.txt').read_text() == 'api-candidate\\n'\n"
                    "assert sys.argv[2] == sys.argv[3]\n"
                    "assert (pathlib.Path(sys.argv[4]) / 'candidate.txt').read_text() == 'mcp-candidate\\n'\n"
                    "print('ARCHON_INTEGRATION_TESTS=1')\n"
                ),
                "${ARCHON_REPO_API_WORKTREE}",
                "${ARCHON_REPO_API_COMMIT}",
                self.api_commit,
                "${ARCHON_REPO_GOODWORD_MCP_WORKTREE}",
            ],
        })
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        report = json.loads((self.artifacts / "joint-integration-result.json").read_text(encoding="utf-8"))
        self.assertEqual("passed", report["status"])
        command = report["commands"][0]
        integration_root = self.artifacts.resolve() / "joint-integration-worktrees"
        self.assertEqual("argv", command["command_kind"])
        self.assertEqual(str(integration_root / "api"), command["cwd"])
        self.assertEqual("api", command["command"]["repo"])
        self.assertEqual(str(integration_root / "api"), command["command"]["argv"][3])
        self.assertEqual(self.api_commit, command["command"]["argv"][4])
        self.assertEqual(str(integration_root / "goodword-mcp"), command["command"]["argv"][6])
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())

    def test_candidates_get_profile_runtime_state_before_commands_and_cleanup_spares_the_source(self):
        # Chain e35b7bd5: candidates had no node_modules/.env.e2e, so the planner
        # invented per-feature bootstrap scripts and the critic capped the loop.
        self.write_plan({
            "repo": "api",
            "argv": [
                "python3", "-c",
                (
                    "import pathlib, sys\n"
                    "cwd = pathlib.Path.cwd()\n"
                    "assert (cwd / 'node_modules' / 'marker').read_text() == 'api'\n"
                    "assert (cwd / '.env.e2e').read_text() == 'api.env.e2e\\n'\n"
                    "assert (pathlib.Path(sys.argv[1]) / 'node_modules' / 'marker').read_text() == 'goodword-mcp'\n"
                    "print('ARCHON_INTEGRATION_TESTS=1')\n"
                ),
                "${ARCHON_REPO_GOODWORD_MCP_WORKTREE}",
            ],
        })
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("CANDIDATE_ENV=PASS repo=api", result.stdout)
        report = json.loads((self.artifacts / "joint-integration-result.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {"node_modules": "linked:" + str(self.root.resolve() / "api" / "node_modules")},
            report["candidate_environments"]["api"]["deps"],
        )
        self.assertEqual([], list(report["candidate_environments"]["goodword-mcp"]["env_files"]))
        # Removing the candidate must unlink, never follow, the links.
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())
        self.assertEqual("api", (self.root / "api" / "node_modules" / "marker").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "api" / ".env.e2e").is_file())

    def test_unresolvable_candidate_environment_fails_before_any_command(self):
        (self.root / "api" / ".env.e2e").unlink()
        self.write_plan("touch command-ran; printf 'ARCHON_INTEGRATION_TESTS=1\\n'")
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("CANDIDATE_ENV=FAIL repo=api .env.e2e: not found in", result.stdout)
        self.assertFalse((self.artifacts / "command-ran").exists())
        self.assertFalse((self.artifacts / "joint-integration-result.json").exists())
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())

    def test_structured_command_rejects_unknown_environment_reference(self):
        self.write_plan({
            "repo": "api",
            "argv": ["python3", "-c", "print('ARCHON_INTEGRATION_TESTS=1')", "${ARCHON_API_CANDIDATE_SHA}"],
        })
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("unknown environment reference", result.stdout)
        self.assertFalse((self.artifacts / "joint-integration-result.json").exists())
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())

    def test_structured_command_rejects_bare_archon_environment_reference(self):
        self.write_plan({
            "repo": "api",
            "argv": ["python3", "-c", "print('ARCHON_INTEGRATION_TESTS=1')", "$ARCHON_REPO_API_WORKTREE"],
        })
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("unsupported bare environment reference", result.stdout)
        self.assertFalse((self.artifacts / "joint-integration-result.json").exists())
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())

    def test_structured_command_rejects_environment_reference_outside_uses(self):
        self.write_plan(
            {"repo": "api", "argv": ["python3", "-c", "print('ARCHON_INTEGRATION_TESTS=1')", "${ARCHON_REPO_GOODWORD_MCP_WORKTREE}"]},
            uses=["api"],
        )
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("references repository outside uses", result.stdout)
        self.assertFalse((self.artifacts / "joint-integration-result.json").exists())
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())

    def test_structured_command_repo_must_be_in_scenario_uses(self):
        self.write_plan(
            {"repo": "goodword-mcp", "argv": ["python3", "-c", "print('ARCHON_INTEGRATION_TESTS=1')"]},
            uses=["api"],
        )
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("repo is not listed in uses", result.stdout)
        self.assertFalse((self.artifacts / "joint-integration-result.json").exists())
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())

    def test_nonzero_command_fails_and_records_evidence(self):
        self.write_plan("printf failed >&2; exit 7")
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("approved integration command failed", result.stdout)
        report = json.loads((self.artifacts / "joint-integration-result.json").read_text(encoding="utf-8"))
        self.assertEqual("failed", report["status"])
        self.assertEqual(7, report["commands"][0]["returncode"])
        log = Path(report["commands"][0]["log"])
        self.assertIn("failed", log.read_text(encoding="utf-8"))

    def test_plan_digest_mismatch_fails_before_running_commands(self):
        self.write_plan("true")
        self.write_candidates(plan_digest="0" * 64)
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("plan_digest does not match", result.stdout)
        self.assertFalse((self.artifacts / "joint-integration-result.json").exists())

    def test_empty_expected_tests_cannot_pass(self):
        self.write_plan("true", expected_tests=[])
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("expected_tests", result.stdout)
        self.assertFalse((self.artifacts / "joint-integration-result.json").exists())

    def test_missing_expected_artifact_fails_with_evidence(self):
        self.write_plan("printf 'ARCHON_INTEGRATION_TESTS=1\\n'", expected_artifacts=["missing-proof.txt"])
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("approved integration command failed", result.stdout)
        report = json.loads((self.artifacts / "joint-integration-result.json").read_text(encoding="utf-8"))
        self.assertEqual("failed", report["status"])
        self.assertEqual("failed", report["tests"][0]["status"])
        self.assertEqual({"bytes": 0, "exists": False, "path": "missing-proof.txt"}, report["tests"][0]["artifacts"][0])

    def test_zero_rc_without_reported_tests_cannot_pass(self):
        self.write_plan("true")
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("approved integration command failed", result.stdout)
        report = json.loads((self.artifacts / "joint-integration-result.json").read_text(encoding="utf-8"))
        self.assertEqual("failed", report["status"])
        self.assertEqual(0, report["tests"][0]["reported_tests"])

    def test_timeout_fails_with_log_and_counter(self):
        self.write_plan("sleep 2", command_timeout_seconds=1)
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        report = json.loads((self.artifacts / "joint-integration-result.json").read_text(encoding="utf-8"))
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["commands"][0]["timed_out"])
        self.assertEqual(1, report["counters"]["timed_out_commands"])
        self.assertIn("JOINT_INTEGRATION_TIMEOUT=1", Path(report["commands"][0]["log"]).read_text(encoding="utf-8"))

    def test_timeout_kills_child_process_before_worktree_cleanup(self):
        self.write_plan(
            "python3 - <<'PY' &\n"
            "import os, pathlib, time\n"
            "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
            "PY\n"
            "wait",
            command_timeout_seconds=1,
        )
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        child_pid = int((self.artifacts / "child.pid").read_text(encoding="utf-8"))
        self.assert_pid_gone(child_pid)
        report = json.loads((self.artifacts / "joint-integration-result.json").read_text(encoding="utf-8"))
        self.assertTrue(report["commands"][0]["timed_out"])
        self.assertIsInstance(report["commands"][0]["launcher_pgid"], int)
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())

    def test_nonzero_launcher_kills_background_child(self):
        self.write_plan(
            "python3 - <<'PY' &\n"
            "import os, pathlib, time\n"
            "pathlib.Path('failure-child.pid').write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
            "PY\n"
            "while [ ! -s failure-child.pid ]; do sleep 0.05; done\n"
            "exit 7",
            command_timeout_seconds=10,
        )
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        child_pid = int((self.artifacts / "failure-child.pid").read_text(encoding="utf-8"))
        self.assert_pid_gone(child_pid)
        report = json.loads((self.artifacts / "joint-integration-result.json").read_text(encoding="utf-8"))
        self.assertEqual(7, report["commands"][0]["returncode"])
        self.assertFalse(report["commands"][0]["timed_out"])

    def test_partial_worktree_creation_failure_cleans_git_metadata(self):
        self.write_plan("true")
        missing_commit = "0" * 40
        self.write_candidates(
            candidate_heads={"api": self.api_commit, "goodword-mcp": missing_commit},
            repositories={
                "api": {"source_worktree": str(self.root / "api"), "commit": self.api_commit},
                "goodword-mcp": {"source_worktree": str(self.root / "goodword-mcp"), "commit": missing_commit},
            },
        )
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("git rev-parse --verify", result.stdout)
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())
        listed = subprocess.check_output(
            ["git", "-C", str(self.root / "api"), "worktree", "list", "--porcelain"],
            env=self.env,
            text=True,
        )
        self.assertNotIn(str(self.artifacts / "joint-integration-worktrees" / "api"), listed)

    def test_rejects_unknown_repository_names(self):
        self.write_plan("true")
        plan = json.loads((self.artifacts / "joint-plan.json").read_text(encoding="utf-8"))
        plan["repositories"] = ["api", "mcp"]
        self.plan_digest = digest(plan)
        write_json(self.artifacts / "joint-plan.json", plan)
        self.write_candidates(repositories={
            "api": {"source_worktree": str(self.root / "api"), "commit": self.api_commit},
            "mcp": {"source_worktree": str(self.root / "goodword-mcp"), "commit": self.mcp_commit},
        }, candidate_heads={"api": self.api_commit, "mcp": self.mcp_commit})
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("unknown: mcp", result.stdout)

    def test_stale_success_result_is_removed_on_early_failure(self):
        write_json(self.artifacts / "joint-integration-result.json", {"status": "passed"})
        self.write_plan("true")
        self.write_candidates(plan_digest="f" * 64)
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertFalse((self.artifacts / "joint-integration-result.json").exists())


    def test_commands_run_under_the_e2e_mutex_and_it_is_released(self):
        self.write_plan(
            "test \"$(cat \"$ARCHON_E2E_LOCK/owner\")\" = \"$ARCHON_EXPECT_OWNER\" || exit 9\n"
            "echo ARCHON_INTEGRATION_TESTS=1")
        self.write_candidates()
        self.env["ARCHON_EXPECT_OWNER"] = str(self.artifacts.resolve())
        result = self.run_runner()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("E2E_MUTEX=ACQUIRED", result.stdout)
        self.assertIn("E2E_MUTEX=RELEASED", result.stdout)
        self.assertFalse(self.lock.exists())

    def test_failed_command_still_releases_the_e2e_mutex(self):
        self.write_plan("exit 7")
        self.write_candidates()
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("E2E_MUTEX=RELEASED", result.stdout)
        self.assertFalse(self.lock.exists())

    def test_live_holder_blocks_commands_until_a_typed_timeout(self):
        marker = self.root / "command-ran"
        self.write_plan(f"touch {marker}; echo ARCHON_INTEGRATION_TESTS=1")
        self.write_candidates()
        self.lock.mkdir()
        (self.lock / "owner").write_text("/elsewhere/runs/live-run\n")
        self.env.update(ARCHON_E2E_WAIT_SECONDS="1", ARCHON_E2E_POLL_SECONDS="0.1")
        result = self.run_runner()
        self.assertEqual(1, result.returncode)
        self.assertIn("E2E_MUTEX=WAITING owner=/elsewhere/runs/live-run", result.stdout)
        self.assertIn("E2E_MUTEX=FAIL timeout", result.stdout)
        self.assertFalse(marker.exists(), "an integration command ran without the e2e mutex")
        self.assertEqual("/elsewhere/runs/live-run", (self.lock / "owner").read_text().strip())
        self.assertFalse((self.artifacts / "joint-integration-worktrees").exists())


if __name__ == "__main__":
    unittest.main()
