#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SETUP = Path(__file__).resolve().parent.parent
ARCHON = SETUP.parent
sys.path.insert(0, str(SETUP))
import layer_root as lr  # noqa: E402


class LayerRootTest(unittest.TestCase):
    def test_discovers_this_pack_when_env_is_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARCHON_LAYER", None)
            self.assertEqual(lr.layer_root(), ARCHON)

    def test_env_override_must_contain_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"ARCHON_LAYER": td}):
                with self.assertRaises(lr.LayerRootError) as ctx:
                    lr.layer_root()
            self.assertIn("LAYER_ROOT=FAIL", str(ctx.exception))

    def test_env_override_with_helpers_wins(self):
        with tempfile.TemporaryDirectory() as td:
            helper = Path(td) / "setup" / "resolve-params.sh"
            helper.parent.mkdir()
            helper.write_text("#!/bin/bash\n")
            with patch.dict(os.environ, {"ARCHON_LAYER": td}):
                self.assertEqual(lr.layer_root(), Path(td).resolve())

    def test_cli_prints_the_path(self):
        env = dict(os.environ)
        env.pop("ARCHON_LAYER", None)
        result = subprocess.run(
            [sys.executable, str(SETUP / "layer_root.py")],
            capture_output=True, text=True, env=env, check=True,
        )
        self.assertEqual(result.stdout.strip(), str(ARCHON))


if __name__ == "__main__":
    unittest.main()
