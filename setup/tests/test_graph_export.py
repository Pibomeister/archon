#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
ARCHON = SETUP.parent
sys.path.insert(0, str(SETUP))
import graph_export as ge  # noqa: E402
import graph_render as gr  # noqa: E402

LITE_API_IDS = json.loads((SETUP / "lite/api.json").read_text())["nodes"]


class GraphExportTest(unittest.TestCase):
    def test_lite_api_exports_manifest_node_ids(self):
        graph = ge.export_workflow(ARCHON / "workflows/full-sdlc-api-lite.yaml")
        self.assertEqual(graph["schema"], "archon.workflow-graph.v1")
        self.assertEqual(graph["workflow"], "full-sdlc-api-lite")
        ids = [n["id"] for n in graph["nodes"] if not n.get("parent")]
        self.assertEqual(ids, LITE_API_IDS)
        preflight = next(n for n in graph["nodes"] if n["id"] == "preflight")
        self.assertEqual(preflight["kind"], "bash")
        self.assertIn("codex-control-guard", preflight["depends_on"])

    def test_all_five_graphs_export(self):
        names = [
            "full-sdlc-api",
            "full-sdlc-api-lite",
            "bugfix",
            "bugfix-lite",
            "full-sdlc-web",
        ]
        for name in names:
            graph = ge.export_workflow(ARCHON / "workflows" / f"{name}.yaml")
            with self.subTest(name=name):
                self.assertEqual(graph["workflow"], name)
                self.assertGreater(len(graph["nodes"]), 3)
                self.assertTrue(any(n["id"] == "preflight" for n in graph["nodes"]))

    def test_render_mermaid_uses_node_ids_not_a_project_name(self):
        graph = ge.export_workflow(ARCHON / "workflows/full-sdlc-api-lite.yaml")
        graph["profileId"] = "project:fluxkeep"
        mermaid = gr.mermaid(graph)
        self.assertIn("flowchart", mermaid)
        self.assertIn("preflight", mermaid)
        self.assertIn("plan-gate", mermaid)
        self.assertNotIn("project:fluxkeep", mermaid.split("flowchart", 1)[1])

    def test_write_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td)
            graph = ge.export_workflow(ARCHON / "workflows/bugfix.yaml")
            ge.write(graph, artifacts)
            saved = json.loads((artifacts / "graph.json").read_text())
            self.assertEqual(saved["workflow"], "bugfix")
            (artifacts / "graph.md").write_text(gr.mermaid(saved), encoding="utf-8")
            self.assertIn("flowchart", (artifacts / "graph.md").read_text())


if __name__ == "__main__":
    unittest.main()
