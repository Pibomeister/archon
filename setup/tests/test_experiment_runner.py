#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "experiment-runner.py"


class ExperimentRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        self.artifacts = self.tmp / "artifacts"
        (self.root / "api" / "src").mkdir(parents=True)
        (self.root / "web-app").mkdir(parents=True)
        (self.root / "api" / "src" / "foo.ts").write_text("export const x = 1\n")

    def spec(self, obj):
        path = self.tmp / "spec.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def run_cli(self, obj, env=None, *, dry_run=True):
        argv = [
            "python3", str(SCRIPT), "--spec", str(self.spec(obj)),
            "--artifacts", str(self.artifacts), "--repo-root", str(self.root),
        ]
        if dry_run:
            argv.append("--dry-run")
        return subprocess.run(argv, capture_output=True, encoding="utf-8", env=env)

    def test_unit_test_adapter_accepts_bounded_argv_not_shell(self):
        r = self.run_cli({"adapter": "unit-test", "repo": "api", "argv": ["bun", "run", "test", "--", "src/__tests__/foo.spec.ts"]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("EXPERIMENT_RUNNER=OK adapter=unit-test", r.stdout)
        result = json.loads((self.artifacts / "experiment-result.json").read_text())
        self.assertEqual(result["result"]["argv"][0], "bun")

    def test_free_form_shell_rejected(self):
        r = self.run_cli({"adapter": "unit-test", "repo": "api", "command": "bun test; curl http://example.com"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("free-form shell command rejected", r.stdout)

    def test_repo_read_is_read_only_and_path_bounded(self):
        r = self.run_cli({"adapter": "repo-read", "repo": "api", "paths": ["src/foo.ts"]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads((self.artifacts / "experiment-result.json").read_text())
        self.assertIn("export const x", data["result"]["files"][0]["content"])
        r = self.run_cli({"adapter": "repo-read", "repo": "api", "paths": ["../web-app/package.json"]})
        self.assertEqual(r.returncode, 1)
        self.assertIn("unsafe path", r.stdout)

    def test_prod_sql_validates_single_read_only_statement_and_wrapper(self):
        env = os.environ.copy()
        env["ARCHON_PROD_SQL_WRAPPER"] = "/bin/echo"
        r = self.run_cli({"adapter": "prod-sql", "sql": "WITH x AS (SELECT 1) SELECT * FROM x"}, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_cli({"adapter": "prod-sql", "sql": "SELECT 1; DROP TABLE users"}, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("one statement", r.stdout)

    def test_timeout_and_argv_bounds(self):
        r = self.run_cli({"adapter": "unit-test", "repo": "api", "argv": ["bun"], "timeout_seconds": 999})
        self.assertEqual(r.returncode, 1)
        self.assertIn("timeout_seconds out of bounds", r.stdout)
        r = self.run_cli({"adapter": "unit-test", "repo": "api", "argv": ["python3", "x.py"]})
        self.assertEqual(r.returncode, 1)
        self.assertIn("must start with", r.stdout)

    def test_real_timeout_kills_process_group_and_writes_typed_result(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        bun = bin_dir / "bun"
        bun.write_text("#!/bin/sh\nsleep 30 &\nwait\n", encoding="utf-8")
        bun.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        r = self.run_cli(
            {"adapter": "unit-test", "repo": "api", "argv": ["bun"], "timeout_seconds": 1},
            env=env,
            dry_run=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        result = json.loads((self.artifacts / "experiment-result.json").read_text())["result"]
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(result["returncode"])

    def test_real_unit_test_sandbox_blocks_repo_writes_and_network(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        bun = bin_dir / "bun"
        bun.write_text(
            "#!/bin/sh\n"
            "touch src/sandbox-escape && exit 70\n"
            "curl --max-time 1 -fsS https://example.com >/dev/null 2>&1 && exit 71\n"
            "echo sandbox-blocked\n",
            encoding="utf-8",
        )
        bun.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        r = self.run_cli(
            {"adapter": "unit-test", "repo": "api", "argv": ["bun"], "timeout_seconds": 5},
            env=env,
            dry_run=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        result = json.loads((self.artifacts / "experiment-result.json").read_text())["result"]
        self.assertEqual(result["returncode"], 0, result)
        self.assertIn("sandbox-blocked", result["output"])
        self.assertFalse((self.root / "api" / "src" / "sandbox-escape").exists())


if __name__ == "__main__":
    unittest.main()
