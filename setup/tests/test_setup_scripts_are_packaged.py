#!/usr/bin/env python3
"""A workflow that calls a setup script the installer does not ship fails at
run time, on the operator's machine, with a bare "No such file or directory" --
and only for the node unlucky enough to need it.

Nothing caught that class before: adding setup/round-reclaim.sh wired four
lanes to a script that package.sh would not have installed. The manifest and
the call sites are edited in different files by different changes, so the drift
is silent until a run dies. Pin them to each other."""
import re
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
MANIFEST = ARCHON / "setup/package.sh"
# Absolute or $ROOT/$SETUP-relative references to a shipped setup script.
REF = re.compile(r'(?:\.archon/setup|\$SETUP)/([A-Za-z0-9_.-]+\.(?:sh|py))')


def manifest_entries():
    return set(re.findall(r'setup/([A-Za-z0-9_.-]+\.(?:sh|py))',
                          MANIFEST.read_text()))


def referenced():
    out = {}
    for p in sorted((ARCHON / "workflows").glob("*.yaml")):
        for name in set(REF.findall(p.read_text())):
            out.setdefault(name, []).append(p.name)
    for p in sorted((ARCHON / "setup/lite").rglob("*")):
        if p.is_file():
            for name in set(REF.findall(p.read_text(errors="replace"))):
                out.setdefault(name, []).append(p.name)
    # A setup script calling another setup script is the same drift with the
    # same symptom, and scanning only the workflows could not see it:
    # port-alloc.sh is reached exclusively from resolve-params.sh.
    for p in sorted((ARCHON / "setup").glob("*.sh")):
        for name in set(REF.findall(p.read_text(errors="replace"))):
            if name != p.name:
                out.setdefault(name, []).append(p.name)
    return out


class SetupScriptsArePackagedTest(unittest.TestCase):
    def test_every_referenced_setup_script_is_in_the_manifest(self):
        shipped = manifest_entries()
        missing = {n: w for n, w in referenced().items() if n not in shipped}
        self.assertEqual(missing, {},
                         "workflows call setup scripts package.sh does not ship")

    def test_the_probe_finds_real_references(self):
        # Negative control for the assertion above: a test that greps nothing
        # passes for the wrong reason forever. Pin a script every lane calls.
        refs = referenced()
        self.assertIn("params-env.sh", refs)
        self.assertGreater(len(refs), 5, refs)

    def test_round_reclaim_is_shipped(self):
        self.assertIn("round-reclaim.sh", manifest_entries())

    def test_canonical_test_runner_is_shipped_and_used_by_package(self):
        package = MANIFEST.read_text(encoding="utf-8")
        self.assertIn("setup/run-tests.py", package)
        self.assertIn('setup/run-tests.py" --start-directory "$ARCHON/setup/tests"', package)
        self.assertIn('--pattern "test_controller_attest.py"', package)

    def test_setup_to_setup_references_are_seen(self):
        # Negative control for the setup/*.sh scan above.
        self.assertIn("port-alloc.sh", referenced())
        self.assertIn("resolve-params.sh", referenced()["port-alloc.sh"])

    def test_package_reference_scanner_ignores_trailing_prose_punctuation(self):
        package = MANIFEST.read_text(encoding="utf-8")
        match = re.search(r"GREP\" -ohE '([^']+)'", package)
        self.assertIsNotNone(match, "package.sh reverse-check grep pattern moved")
        ref_pattern = match.group(1)
        text = "Run $SETUP/params-env.sh. Then call setup/round-reclaim.sh, safely."
        refs = {
            re.sub(r"^\$\{?SETUP\}?/", "setup/", m.group(0))
            for m in re.finditer(ref_pattern, text)
        }
        self.assertEqual(refs, {"setup/params-env.sh", "setup/round-reclaim.sh"})


if __name__ == "__main__":
    unittest.main()
