#!/usr/bin/env python3
import hashlib
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
AUDIT_LOG = ROOT / ".archon/audit/hardening/backfill-db-matrix.log"
DEFAULT_API = ROOT / "api"

DB_SPEC_REL = Path("libs/data-access/src/lib/rds/backfills/__tests__/trusted-backfill-executor.db.spec.ts")
EXECUTOR_REL = Path("libs/data-access/src/lib/rds/backfills/trusted-backfill-executor.ts")
LEDGER_MIGRATION_REL = Path("libs/data-access/src/lib/rds/migrations/1791980000001-archon-backfill-ledger.ts")
JEST_CONFIGS = (Path("jest.config.js"),)

DB_MATRIX_OUTPUT_MARKERS = (
    "DB_CLUSTER_SOURCE=pg_control_system",
    "LEDGER_MIGRATION=preexisting-refused",
    "TYPEORM_RETURNING_SHAPE=rows-count-tuple",
    "SCALAR_FIDELITY=timestamp-refused",
    "SCALAR_FIDELITY=jsonb-refused",
    "SCALAR_FIDELITY=float-refused",
    "SCALAR_FIDELITY=date-scalar-refused",
    "KEY_ADMISSION=nonprimary-refused",
    "KEY_ADMISSION=composite-pk-ok",
    "TX_BOUNDARY=outer-transaction-refused",
    "SCHEMA_PROVENANCE=catalog-enum-refused",
    "SCHEMA_PROVENANCE=catalog-opclass-refused",
    "READ_PHASE_GUARD=standby-ok",
    "READ_PHASE_GUARD=write-role-refused",
    "READ_PHASE_GUARD=global-write-refused",
    "CANCELLATION=wrong-cluster-not-mutated",
    "CANCELLATION=between-chunks-persisted",
    "CANCELLATION=row-lock-timeout-rolled-back",
    "CANCELLATION=active-row-lock-canceled",
    "CANCELLATION=active-partial-chunk-preserved",
    "CANCELLATION=post-complete-update-rolled-back",
    "NEGATIVE_CONTROL=guard-disabled-float-loss-accepted",
    "DB_MATRIX=PASS",
)

FORBIDDEN_RUNTIME_ENV_PREFIXES = (
    "AWS_",
    "GH_",
    "GITHUB_",
    "OPENAI_",
    "ANTHROPIC_",
    "DATABASE_",
)
FORBIDDEN_RUNTIME_ENV_NAMES = {"RW_DSN", "RO_DSN", "API_KEY", "DATABASE_URL"}


def api_root() -> Path:
    return Path(os.environ.get("ARCHON_BACKFILL_API_ROOT", DEFAULT_API)).expanduser().resolve()


def api_path(relative: Path) -> Path:
    return api_root() / relative


def enabled_e2e() -> bool:
    return os.environ.get("ARCHON_BACKFILL_DB_MATRIX_E2E") == "1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_value(args: list[str]) -> str:
    return subprocess.check_output(["git", "-C", str(api_root()), *args], text=True).strip()


def source_identity_lines() -> list[str]:
    lines = [
        f"ARCHON_BACKFILL_API_ROOT={api_root()}",
        f"ARCHON_BACKFILL_API_HEAD={git_value(['rev-parse', 'HEAD'])}",
        f"ARCHON_BACKFILL_API_BRANCH={git_value(['branch', '--show-current'])}",
        f"ARCHON_BACKFILL_API_STATUS={git_value(['status', '--short']) or 'clean'}",
    ]
    for relative in (DB_SPEC_REL, EXECUTOR_REL, LEDGER_MIGRATION_REL):
        lines.append(f"ARCHON_BACKFILL_SOURCE_SHA256 {relative}={sha256(api_path(relative))}")
    return lines


def allowlisted_env() -> dict[str, str]:
    names = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "SystemRoot")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["ARCHON_BACKFILL_DB_TEST"] = "1"
    env["CI"] = "1"
    return env


def assert_no_forbidden_env(env: dict[str, str]) -> None:
    for name in env:
        if name in FORBIDDEN_RUNTIME_ENV_NAMES or name.startswith(FORBIDDEN_RUNTIME_ENV_PREFIXES):
            raise AssertionError(f"forbidden inherited runtime environment variable: {name}")


def write_matrix_log(text: str) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_LOG.write_text(text, encoding="utf-8")


def run_matrix() -> subprocess.CompletedProcess[str]:
    env = allowlisted_env()
    assert_no_forbidden_env(env)
    return subprocess.run(
        [
            str(api_path(Path("node_modules/.bin/jest"))),
            str(DB_SPEC_REL),
            "--runInBand",
            "--verbose",
        ],
        cwd=api_root(),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def require_e2e_prerequisites() -> None:
    missing = []
    if shutil.which("docker") is None:
        missing.append("docker CLI")
    for relative in (DB_SPEC_REL, EXECUTOR_REL, LEDGER_MIGRATION_REL, Path("node_modules/.bin/jest")):
        if not api_path(relative).is_file():
            missing.append(str(api_path(relative)))
    if missing:
        raise AssertionError("opt-in PostgreSQL matrix prerequisites missing: " + ", ".join(missing))


class BackfillDisposablePostgresMatrixTest(unittest.TestCase):
    def test_api_backfill_matrix_sources_are_present_at_selected_root(self):
        for relative in (DB_SPEC_REL, EXECUTOR_REL, LEDGER_MIGRATION_REL):
            with self.subTest(path=str(relative)):
                self.assertTrue(api_path(relative).is_file())

    def test_jest_configs_do_not_load_dotenv_for_this_matrix(self):
        for relative in JEST_CONFIGS:
            path = api_path(relative)
            self.assertTrue(path.is_file(), str(path))
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=str(relative)):
                self.assertNotIn("dotenv", source)
                self.assertNotIn("setupFiles", source)

    def test_database_matrix_subprocesses_use_owned_disposable_docker_resources(self):
        source = api_path(DB_SPEC_REL).read_text(encoding="utf-8")
        required_guards = (
            "archon-backfill-test-owner",
            "--internal",
            "verifyOwnedContainer",
            "verifyOwnedNetwork",
            "verifyOwnedVolume",
            "assertNoBindMounts",
            "assertNoPublishedPorts",
            "assertOwnedFixtureResourcesRemoved",
            "node:22-alpine",
            "postgres:16-alpine",
        )
        for guard in required_guards:
            with self.subTest(guard=guard):
                self.assertIn(guard, source)


    def test_subprocess_environment_drops_credentials_and_dsns(self):
        original = {
            key: os.environ.get(key)
            for key in ("AWS_SECRET_ACCESS_KEY", "GH_TOKEN", "DATABASE_URL", "RW_DSN")
        }
        try:
            os.environ["AWS_SECRET_ACCESS_KEY"] = "synthetic"
            os.environ["GH_TOKEN"] = "synthetic"
            os.environ["DATABASE_URL"] = "postgres://synthetic"
            os.environ["RW_DSN"] = "postgres://synthetic"
            env = allowlisted_env()
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("DATABASE_URL", env)
            self.assertNotIn("RW_DSN", env)
            assert_no_forbidden_env(env)
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_real_database_matrix_passes_when_explicitly_enabled(self):
        if not enabled_e2e():
            self.skipTest("set ARCHON_BACKFILL_DB_MATRIX_E2E=1 to run the disposable PostgreSQL matrix")
        require_e2e_prerequisites()
        identity = "\n".join(source_identity_lines())
        result = run_matrix()
        output = identity + "\n" + result.stdout + result.stderr
        write_matrix_log(output)
        self.assertEqual(result.returncode, 0, output)
        for marker in DB_MATRIX_OUTPUT_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, output)


if __name__ == "__main__":
    unittest.main()
