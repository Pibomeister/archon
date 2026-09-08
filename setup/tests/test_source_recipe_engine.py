"""Actual source repos/worktrees and checks; all agent judgments are explicit stubs."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import unittest
import uuid

import yaml
# Sibling test modules are imported by bare name, the way every other module in
# this suite does it: the canonical invocation is `cd setup && python3 -m unittest
# discover -s tests`, which puts tests/ on sys.path but NOT the repo root, so
# `from setup.tests import ...` raises ModuleNotFoundError at import time and
# turns these deliberate skipUnless integration tests into hard suite ERRORS.
import test_portable_engine as engine_fixtures

ENGINE_SOURCE = os.environ.get("ENGINE_SOURCE_REPO")
GOODWORD_SOURCE = os.environ.get("GOODWORD_SOURCE_REPO")
UV_BINARY = os.environ.get("QUALIFIED_UV_BINARY")
UV_CACHE = os.environ.get("QUALIFIED_UV_CACHE")
PYTHON_INSTALL = os.environ.get("QUALIFIED_PYTHON_INSTALL_DIR")


@unittest.skipUnless(ENGINE_SOURCE and GOODWORD_SOURCE and UV_BINARY and UV_CACHE and PYTHON_INSTALL, "Explicit qualified source/tool paths required")
class SourceRecipeEngineTest(engine_fixtures.PortableEngineTest):
    # Do not inherit the three generic fixture tests into this source-repo suite.
    test_production_graph_loads_with_qualified_engine = None
    test_native_gate_resume_executes_only_mechanics_and_records_publication_hold = None
    test_editing_plan_and_local_checksum_cannot_change_engine_approved_original = None

    def run_owned(self, command, cwd, env, timeout):
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def cli(self, fixture, *args, json_output=False):
        command = [*fixture.engine, "workflow", *args, "--cwd", str(fixture.worktree), *(["--json"] if json_output else [])]
        return self.run_owned(command, fixture.worktree, fixture.env, 1500)

    def source_fixture(self, recipe):
        fixture = self.fixture()
        source = Path(ENGINE_SOURCE if recipe == "engine" else GOODWORD_SOURCE).resolve()
        base = "85fe6e2f4c6ffca60a77478ac0698ef4b6da4b5f" if recipe == "engine" else fixture.git(source, "rev-parse", "HEAD")
        branch = "factory/source-recipe-qualification-" + uuid.uuid4().hex[:12]
        target = fixture.allowed / "source-recipe"
        fixture.git(source, "worktree", "add", "-q", "-b", branch, str(target), base)
        def cleanup():
            fixture.git(source, "worktree", "remove", "--force", str(target))
            fixture.git(source, "branch", "-D", branch)
        self.addCleanup(cleanup)
        fixture.worktree = target
        fixture.base = base
        fixture.actual_source = source
        name = "archon-engine.v2.json" if recipe == "engine" else "goodword-archon.v2.json"
        fixture.profile = json.loads((Path(GOODWORD_SOURCE) / "profiles" / name).read_text())
        fixture.profile_path.write_text(json.dumps(fixture.profile))
        fixture.binding.update(projectId=fixture.profile["projectId"], repositoryRoot=str(target), branch=branch, baseCommit=base, profileSha256=engine_fixtures.portable.file_digest(fixture.profile_path), knowledgeRoot=str(source), knowledgeCommit=base)
        fixture.save_binding()
        # This is a non-model qualification process, not an enrolled coding agent.
        env = {key: value for key, value in fixture.env.items() if not any(word in key.upper() for word in ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "COOKIE"))}
        env.update(HOME=str(fixture.root / "private-home"), CODEX_HOME=str(fixture.root / "private-codex"), CLAUDE_CONFIG_DIR=str(fixture.root / "private-claude"), HERMES_HOME=str(fixture.root / "private-hermes"), PATH=str(Path(UV_BINARY).parent) + os.pathsep + os.environ["PATH"], UV_CACHE_DIR=str(UV_CACHE), UV_PYTHON_INSTALL_DIR=str(PYTHON_INSTALL), UV_PYTHON="3.13.9", ARCHON_TEST_PG_URL="")
        Path(env["HOME"]).mkdir()
        # Upstream tests set their own temporary HOME to exercise default skill
        # discovery. A fixed CLAUDE_CONFIG_DIR overrides those fixtures. The
        # private HOME already isolates all default provider credentials.
        env.pop("CLAUDE_CONFIG_DIR", None)
        fixture.env = env
        if recipe == "engine":
            install = self.run_owned([shutil.which("bun"), "install", "--frozen-lockfile"], target, {**env, "HUSKY": "0"}, 300)
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        return fixture

    def source_graph(self, fixture, marker):
        workflow = self.mechanical_graph(fixture)
        def replace(nodes):
            for node in nodes:
                if "loop_group" in node:
                    replace(node["loop_group"]["nodes"])
                if "script" in node and isinstance(node["script"], str) and "view.txt" in node["script"]:
                    node["script"] = node["script"].replace("view.txt", marker)
                    if node["id"] == "implement":
                        node["script"] = "import json,os\nfrom pathlib import Path\nb=json.loads(Path(os.environ['INPUTS_BINDING']).read_text())\np=Path(b['repositoryRoot'])/" + repr(marker) + "\np.write_text(p.read_text()+'\\n<!-- deterministic source-profile qualification -->\\n')\nprint(json.dumps({'summary':'Deterministic source fixture; no model invocation','changes':[" + repr(marker) + "],'testNotes':[]}))\n"
        replace(workflow["nodes"])
        (fixture.pack / "feature.yaml").write_text(yaml.safe_dump(workflow, sort_keys=False))

    def exercise_source(self, recipe):
        fixture = self.source_fixture(recipe)
        marker = "README.md" if recipe == "engine" else "RUNBOOK.md"
        self.source_graph(fixture, marker)
        start = self.cli(fixture, "run", "portable-mechanical-qualification", "--workflow-source", str(fixture.authoring), "--input", "binding=" + str(fixture.binding_path), "--launch-key", "source-recipe-1")
        self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
        state = json.loads(self.cli(fixture, "launch-status", "source-recipe-1", json_output=True).stdout)
        self.assertEqual(state["status"], "paused")
        run_id = state["receipt"]["runId"]
        response = json.loads(self.cli(fixture, "get", run_id, json_output=True).stdout)
        run = response.get("run", response)
        gate = run["metadata"]["approval"]
        artifacts = Path(run["output_root"]) / "artifacts/runs" / run_id
        context = json.loads((artifacts / "run-context.json").read_text())
        self.assertEqual(context["profile"]["profileVersion"], "archon.project-profile.v2")
        self.assertNotIn("packageManager", context["profile"]["repository"])
        decision = self.cli(fixture, "respond", run_id, "approve", "--command-id", "source-recipe-test-approval", "--expected-occurrence", gate["occurrenceId"], "--expected-evidence-digest", gate["evidenceDigest"], json_output=True)
        self.assertEqual(decision.returncode, 0, decision.stdout + decision.stderr)
        resumed = self.cli(fixture, "resume", run_id)
        self.assertNotEqual(resumed.returncode, 0)
        if "Draft publication not authorized" not in resumed.stdout + resumed.stderr:
            for log in sorted((artifacts / "verification").glob("*.log")):
                print(log.name + "\n" + log.read_text()[-18000:])
        self.assertIn("Draft publication not authorized", resumed.stdout + resumed.stderr)
        verification = json.loads((artifacts / "verification.json").read_text())
        self.assertTrue(verification["passed"])
        self.assertTrue(all(command["exitCode"] == 0 for command in verification["commands"]))
        self.assertEqual(fixture.git(fixture.worktree, "rev-parse", "HEAD"), fixture.base)
        evidence = {"recipe": recipe, "sourceCommit": fixture.base, "workflowSourceCommit": fixture.git(Path(GOODWORD_SOURCE), "rev-parse", "HEAD"), "toolchain": context["toolchain"], "checks": [{"id": item["id"], "exitCode": item["exitCode"]} for item in verification["commands"]], "modelsInvoked": False, "publication": "blocked-as-required"}
        print(json.dumps(evidence, sort_keys=True))

    def test_actual_engine_source_recipe(self):
        self.exercise_source("engine")

    def test_actual_python_workflow_source_recipe(self):
        self.exercise_source("goodword")
