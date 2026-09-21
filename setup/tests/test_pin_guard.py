#!/usr/bin/env python3
"""pin-guard.py reads the STAGED blob, because that is what `git commit` writes.

v1's api stage reviewed the same two widened methods for four rounds against a
spec sentence nothing enforced. The guard exists so that sentence has a reader.
Reading the index rather than the worktree is the load-bearing part: an agent
that edits a file after staging, or stages a change it then reverts on disk,
must not be able to move a pinned symbol past the gate either way.
"""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
PIN_GUARD = ARCHON / "setup" / "pin-guard.py"

SOURCE = """import { Injectable } from '@nestjs/common';

@Injectable()
export class GroupService {
  private readonly links = 1;

  @Post('share')
  @UseGuards(
    AuthGuard,
  )
  async shareGroup(
    dto: ShareDto,
    user: User,
  ): Promise<{ url: string }> {
    const link = { a: '}' };
    return { url: link.a };
  }

  reshareGroup = async (id: string): Promise<void> => {
    await this.links;
  };
}

export function helper(a: number) {
  return a + 1;
}
"""

PYTHON_SOURCE = """import os


class Thing:
    @property
    def pinned(self):
        value = os.getpid()
        return value

    def other(self):
        return 2
"""


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ad = self.root / "artifacts"
        self.wt = self.root / "wt"
        (self.wt / "src").mkdir(parents=True)
        self.ad.mkdir()
        self.git("init", "-q", "-b", "main", ".")
        self.git("config", "user.email", "pin@test")
        self.git("config", "user.name", "pin")
        self.path = "src/group.service.ts"
        self.write(SOURCE)
        self.git("add", "-A")
        self.git("commit", "-qm", "seed")
        self.baseline = self.git("rev-parse", "HEAD")
        (self.ad / "params.json").write_text(
            json.dumps({"spec": "s", "slug": "s", "branch": "main",
                        "worktree": str(self.wt), "repo": "api"}), encoding="utf-8")
        self.pin("shareGroup", self.path, "none")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.wt), *args], capture_output=True,
                              encoding="utf-8", check=True).stdout.strip()

    def write(self, text, path=None):
        (self.wt / (path or self.path)).write_text(text, encoding="utf-8")

    def pin(self, symbol, path, allowed="none", rule="No other change"):
        (self.ad / "joint-plan.json").write_text(json.dumps({
            "pinned_decisions": [{"symbol": symbol, "file": path, "rule": rule,
                                  "spec_line": 12, "allowed_change": allowed}]}),
            encoding="utf-8")

    def guard(self):
        self.git("add", "-A")
        return subprocess.run([sys.executable, str(PIN_GUARD), str(self.ad), self.baseline],
                              capture_output=True, encoding="utf-8")

    def spans(self):
        return json.loads((self.ad / "pin-spans.json").read_text(encoding="utf-8"))


class TypeScript(GuardCase):
    def test_an_untouched_symbol_is_ok(self):
        proc = self.guard()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("PIN_OK", proc.stdout)
        self.assertEqual(self.spans()[0]["result"], "PIN_OK")

    def test_a_body_edit_with_allowed_change_none_is_a_breach(self):
        self.write(SOURCE.replace("const link = { a: '}' };", "const link = { a: 'x' };"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_BREACH symbol=shareGroup", proc.stdout)

    def test_a_body_edit_with_a_stated_exception_is_informational(self):
        self.pin("shareGroup", self.path, "the managed-group sentence only")
        self.write(SOURCE.replace("const link = { a: '}' };", "const link = { a: 'x' };"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("PIN_CHANGED symbol=shareGroup allowed=the managed-group sentence only",
                      proc.stdout)

    def test_a_signature_change_counts_as_a_body_change(self):
        self.write(SOURCE.replace("    user: User,\n", "    user: User,\n    extra: boolean,\n"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_BREACH", proc.stdout)

    def test_a_decorator_only_change_is_seen(self):
        self.pin("shareGroup", self.path, "auth guard may be swapped")
        self.write(SOURCE.replace("AuthGuard,", "AuthGuard, RoleGuard,"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("PIN_CHANGED", proc.stdout)

    def test_the_absolute_pin_is_recognised_however_it_is_spelled(self):
        """The one value that means "no exception" must not fail open."""
        for spelling in ("none", "None", "NONE", "  none  ", "", None):
            with self.subTest(allowed=spelling):
                self.pin("shareGroup", self.path, spelling)
                self.write(SOURCE.replace("const link = { a: '}' };",
                                          "const link = { a: 'x' };"))
                proc = self.guard()
                self.assertEqual(proc.returncode, 1, f"{spelling!r}: {proc.stdout}")
                self.assertIn("PIN_BREACH", proc.stdout)

    def test_a_missing_symbol_is_unresolved_never_a_pass(self):
        self.pin("noSuchMethod", self.path)
        proc = self.guard()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_UNRESOLVED symbol=noSuchMethod", proc.stdout)

    def test_a_renamed_file_is_unresolved(self):
        self.pin("shareGroup", "src/gone.service.ts")
        proc = self.guard()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_UNRESOLVED", proc.stdout)
        self.assertIn("file missing in baseline", proc.stdout)

    def test_the_staged_content_decides_not_the_worktree(self):
        """Stage the breach, then revert the worktree: the commit still carries it."""
        self.write(SOURCE.replace("const link = { a: '}' };", "const link = { a: 'x' };"))
        self.git("add", "-A")
        self.write(SOURCE)  # worktree looks innocent again
        proc = subprocess.run([sys.executable, str(PIN_GUARD), str(self.ad), self.baseline],
                              capture_output=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_BREACH", proc.stdout)

    def test_an_unstaged_breach_does_not_fire(self):
        """The mirror case: edited on disk, never staged, so nothing is committed."""
        self.guard()  # stage the clean state
        self.write(SOURCE.replace("const link = { a: '}' };", "const link = { a: 'x' };"))
        proc = subprocess.run([sys.executable, str(PIN_GUARD), str(self.ad), self.baseline],
                              capture_output=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_an_edit_elsewhere_in_the_file_does_not_touch_the_pin(self):
        self.write(SOURCE.replace("  return a + 1;", "  return a + 2;"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_an_arrow_property_is_extracted(self):
        self.pin("reshareGroup", self.path)
        self.write(SOURCE.replace("await this.links;", "await this.links; // widened"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_BREACH symbol=reshareGroup", proc.stdout)

    def test_a_free_function_is_extracted(self):
        self.pin("helper", self.path)
        self.write(SOURCE.replace("  return a + 1;", "  return a + 2;"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_BREACH symbol=helper", proc.stdout)

    def test_the_span_covers_decorators_through_the_closing_brace(self):
        self.guard()
        span = self.spans()[0]["baseline_span"]
        body = SOURCE[span[0]:span[1]]
        self.assertTrue(body.lstrip().startswith("@Post('share')"), body[:40])
        self.assertTrue(body.rstrip().endswith("}"), body[-40:])
        self.assertIn("return { url: link.a };", body)
        self.assertNotIn("reshareGroup", body)

    def test_spans_are_written_for_diagnosis(self):
        self.guard()
        record = self.spans()[0]
        self.assertEqual(record["symbol"], "shareGroup")
        self.assertEqual(record["file"], self.path)
        self.assertIn("baseline_span", record)
        self.assertIn("staged_span", record)


class Python(GuardCase):
    def setUp(self):
        super().setUp()
        self.path = "src/thing.py"
        self.write(PYTHON_SOURCE)
        self.git("add", "-A")
        self.git("commit", "-qm", "python seed")
        self.baseline = self.git("rev-parse", "HEAD")
        self.pin("pinned", self.path)

    def test_an_untouched_def_is_ok(self):
        proc = self.guard()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_a_body_edit_is_a_breach(self):
        self.write(PYTHON_SOURCE.replace("value = os.getpid()", "value = os.getppid()"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_BREACH symbol=pinned", proc.stdout)

    def test_a_sibling_def_is_outside_the_span(self):
        self.write(PYTHON_SOURCE.replace("        return 2", "        return 3"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_the_decorator_is_part_of_the_span(self):
        self.write(PYTHON_SOURCE.replace("    @property\n", "    @cached_property\n"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_BREACH", proc.stdout)


REGEX_SOURCE = """export class Sanitizer {
  @Post('clean')
  clean(a: string): string {
    const stripped = a.replace(/}/g, '');
    return stripped.replace(/[{}]/g, '');
  }

  other() {
    return 1;
  }
}
"""

UNBALANCED_SOURCE = """export class Broken {
  clean(a: string): string {
    return a.replace('}', '');
"""


class RegexLiterals(GuardCase):
    """A regex literal carrying a brace must not walk the span off the end.

    `a.replace(/}/g, '')` is an unbalanced `}` to a brace matcher that only
    knows strings and comments. The span truncated mid-body, both sides
    truncated the same way, they compared equal, and an edited pinned body
    reported PIN_OK. That is the one outcome this guard must never produce by
    accident: a breach reported as held.
    """

    def setUp(self):
        super().setUp()
        self.path = "src/sanitizer.ts"
        self.write(REGEX_SOURCE)
        self.git("add", "-A")
        self.git("commit", "-qm", "regex seed")
        self.baseline = self.git("rev-parse", "HEAD")
        self.pin("clean", self.path)

    def test_an_untouched_body_with_a_regex_literal_is_ok(self):
        proc = self.guard()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("PIN_OK", proc.stdout)

    def test_a_body_edit_behind_a_regex_literal_is_a_breach(self):
        self.write(REGEX_SOURCE.replace("const stripped = a.replace(/}/g, '');",
                                        "const stripped = a.replace(/}/g, 'x');"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("PIN_BREACH symbol=clean", proc.stdout)

    def test_the_span_stops_at_the_methods_own_brace(self):
        self.guard()
        span = self.spans()[0]["baseline_span"]
        body = REGEX_SOURCE[span[0]:span[1]]
        self.assertIn("@Post('clean')", body)
        self.assertIn("[{}]", body)
        self.assertNotIn("other()", body)

    def test_a_division_is_not_read_as_a_regex(self):
        """`/` after an operand divides. Reading it as a regex would blank out
        the rest of the body and truncate the span just as badly."""
        self.write(REGEX_SOURCE.replace("return stripped.replace(/[{}]/g, '');",
                                        "return (a.length / 2) + stripped;"))
        self.git("add", "-A")
        self.git("commit", "-qm", "division")
        self.baseline = self.git("rev-parse", "HEAD")
        proc = self.guard()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("PIN_OK", proc.stdout)
        self.assertIn("+ stripped;", REGEX_SOURCE.replace(
            "return stripped.replace(/[{}]/g, '');",
            "return (a.length / 2) + stripped;")[slice(*self.spans()[0]["baseline_span"])])


class UnbalancedSpan(GuardCase):
    def test_a_body_that_never_closes_is_unresolved_never_ok(self):
        """A truncated span compares two half-bodies and can report PIN_OK on a
        real breach. No span at all is the safe answer."""
        self.path = "src/broken.ts"
        self.write(UNBALANCED_SOURCE)
        self.git("add", "-A")
        self.git("commit", "-qm", "unbalanced")
        self.baseline = self.git("rev-parse", "HEAD")
        self.pin("clean", self.path)
        proc = self.guard()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("PIN_UNRESOLVED symbol=clean", proc.stdout)
        self.assertNotIn("PIN_OK", proc.stdout)


class CommentedDefinition(GuardCase):
    def test_a_commented_out_definition_does_not_win_the_search(self):
        """An old copy in a block comment above the live one would pin the wrong
        body, and every later edit to the real method would read as unchanged."""
        source = ("/*\n  async shareGroup(old: Dto) {\n    return 0;\n  }\n*/\n"
                  + SOURCE)
        self.write(source)
        self.git("add", "-A")
        self.git("commit", "-qm", "commented copy")
        self.baseline = self.git("rev-parse", "HEAD")
        self.write(source.replace("const link = { a: '}' };", "const link = { a: 'x' };"))
        proc = self.guard()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("PIN_BREACH symbol=shareGroup", proc.stdout)


class DecoratorRun(GuardCase):
    def test_a_multi_line_call_above_the_decorators_stays_out_of_the_span(self):
        """The walk goes up through `)` lines to reach `@UseGuards(`; without
        checking who opened each group it swallows the statement above too."""
        source = SOURCE.replace(
            "  @Post('share')",
            "  private readonly cfg = build(\n    'a',\n  );\n  @Post('share')")
        self.write(source)
        self.git("add", "-A")
        self.git("commit", "-qm", "call above decorators")
        self.baseline = self.git("rev-parse", "HEAD")
        self.guard()
        body = source[slice(*self.spans()[0]["baseline_span"])]
        self.assertTrue(body.lstrip().startswith("@Post('share')"), body[:60])
        self.assertNotIn("build(", body)


class Ambiguity(GuardCase):
    def test_two_definitions_of_one_name_resolve_to_unresolved(self):
        """An overload set or a reused name: guessing the span is the worse failure."""
        self.write(SOURCE + "\nexport function helper(a: string) {\n  return a;\n}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "overload")
        self.baseline = self.git("rev-parse", "HEAD")
        self.pin("helper", self.path)
        proc = self.guard()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PIN_UNRESOLVED symbol=helper", proc.stdout)


CONTROLLER_SOURCE = """import { Controller, Get, Post, UseGuards } from '@nestjs/common';

@Controller('group')
export class GroupController {
  @Get(':id')
  async getGroup(@Param('id') id: string) {
    return this.service.get(id);
  }

  @UseGuards(CompositeAuthGuard)
  @Get(':groupId/share-link')
  async getShareLink(@Param('groupId') groupId: string) {
    return this.service.shareLink(groupId);
  }

  @Post('share')
  async share() {
    return 1;
  }
}

export class GetGroupShareLinkResponseDto {
  url: string | null;
  expiresAt: string | null;
}
"""


class SpecVocabulary(GuardCase):
    """Run 9fd801f3 (2026-09-17): every one of the planner's 15 pins was
    PIN_UNRESOLVED. The spec names symbols as `Class.member`, routes, DTO
    classes, whole files and MCP tool ids, and half of them belonged to the
    other repository's stage."""

    def setUp(self):
        super().setUp()
        self.ctl = "src/group.controller.ts"
        self.write(CONTROLLER_SOURCE, self.ctl)
        self.git("add", "-A")
        self.git("commit", "-qm", "controller")
        self.baseline = self.git("rev-parse", "HEAD")

    def plan(self, pins, stages=None, own_allowlist=None):
        doc = {"pinned_decisions": pins}
        if stages:
            doc["stages"] = stages
        (self.ad / "joint-plan.json").write_text(json.dumps(doc), encoding="utf-8")
        if own_allowlist is not None:
            (self.ad / "files-allowlist.json").write_text(json.dumps(own_allowlist), encoding="utf-8")

    def entry(self, symbol, path, allowed="none"):
        return {"symbol": symbol, "file": path, "rule": "r", "spec_line": 1, "allowed_change": allowed}

    def test_a_qualified_member_resolves_inside_its_class(self):
        self.plan([self.entry("GroupController.getShareLink", self.ctl)])
        self.assertIn("PIN_OK pins=1", self.guard().stdout)
        self.write(CONTROLLER_SOURCE.replace("shareLink(groupId)", "shareLink(groupId, true)"), self.ctl)
        p = self.guard()
        self.assertIn("PIN_BREACH symbol=GroupController.getShareLink", p.stdout)
        self.assertEqual(p.returncode, 1)

    def test_a_route_resolves_to_the_handler_under_its_decorator(self):
        self.plan([self.entry("GET /group/:groupId/share-link", self.ctl)])
        self.assertIn("PIN_OK pins=1", self.guard().stdout)
        span = self.spans()[0]["staged_span"]
        body = CONTROLLER_SOURCE[span[0]:span[1]]
        self.assertTrue(body.lstrip().startswith("@UseGuards(CompositeAuthGuard)"), body)
        self.assertIn("getShareLink", body)
        self.assertNotIn("getGroup(", body)
        # An edit to a sibling handler does not touch the route's pin.
        self.write(CONTROLLER_SOURCE.replace("return 1;", "return 2;"), self.ctl)
        self.assertIn("PIN_OK pins=1", self.guard().stdout)
        # The handler's own body does.
        self.write(CONTROLLER_SOURCE.replace("shareLink(groupId)", "shareLink(groupId, 1)"), self.ctl)
        self.assertIn("PIN_BREACH symbol=GET /group/:groupId/share-link", self.guard().stdout)

    def test_a_class_declaration_is_a_span(self):
        self.plan([self.entry("GetGroupShareLinkResponseDto", self.ctl)])
        self.assertIn("PIN_OK pins=1", self.guard().stdout)
        self.write(CONTROLLER_SOURCE.replace("expiresAt: string | null;", "expiresAt: string;"), self.ctl)
        self.assertIn("PIN_BREACH symbol=GetGroupShareLinkResponseDto", self.guard().stdout)

    def test_a_symbol_this_change_introduces_is_new_not_unresolved(self):
        self.plan([self.entry("NewThingDto", self.ctl), self.entry("POST /group/archive", self.ctl)])
        self.write(CONTROLLER_SOURCE + "\nexport class NewThingDto { a: number; }\n", self.ctl)
        p = self.guard()
        self.assertIn("PIN_NEW symbol=NewThingDto", p.stdout)
        # Named by the plan, still absent from the staged blob: that is a stop.
        self.assertIn("PIN_UNRESOLVED symbol=POST /group/archive", p.stdout)
        self.assertEqual(p.returncode, 1)

    def test_a_new_file_named_by_a_pin_is_new(self):
        self.plan([self.entry("registerShareGroup", "src/share-group.ts")])
        self.write("export function registerShareGroup() {\n  return 1;\n}\n", "src/share-group.ts")
        p = self.guard()
        self.assertIn("PIN_NEW symbol=registerShareGroup", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_a_pin_on_a_file_the_plan_changes_is_unenforceable_elsewhere_it_freezes_the_file(self):
        self.plan([self.entry(self.ctl, self.ctl)], own_allowlist=[self.ctl])
        self.write(CONTROLLER_SOURCE.replace("return 1;", "return 2;"), self.ctl)
        p = self.guard()
        self.assertIn("PIN_UNENFORCEABLE symbol=src/group.controller.ts", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout)
        # Negative control: the same file pin outside the allowlist is a whole-file freeze.
        self.plan([self.entry("group.controller.ts", self.ctl)], own_allowlist=["src/other.ts"])
        p = self.guard()
        self.assertIn("PIN_BREACH symbol=group.controller.ts", p.stdout)
        self.assertEqual(p.returncode, 1)

    def test_a_tool_id_named_only_as_a_literal_is_unenforceable(self):
        self.write("export const tool = { name: 'share_group', run: () => 1 };\n", "src/tool.ts")
        self.git("add", "-A"); self.git("commit", "-qm", "tool")
        self.baseline = self.git("rev-parse", "HEAD")
        self.plan([self.entry("share_group", "src/tool.ts"), self.entry("no_such_thing", "src/tool.ts")])
        p = self.guard()
        self.assertIn("PIN_UNENFORCEABLE symbol=share_group", p.stdout)
        self.assertIn("PIN_UNRESOLVED symbol=no_such_thing", p.stdout)
        self.assertEqual(p.returncode, 1)

    def test_another_stages_pins_are_not_checked_against_this_worktree(self):
        stages = {"api": {"files_allowlist": [self.ctl]},
                  "goodword-mcp": {"files_allowlist": ["src/tools/share-group.ts"]}}
        self.plan([self.entry("share_group", "src/tools/share-group.ts"),
                   self.entry("GroupController.getGroup", self.ctl)], stages=stages)
        p = self.guard()
        self.assertIn("PIN_OK pins=2 changed=0 new=0 unenforceable=0 other_repo=1", p.stdout)
        self.assertEqual(self.spans()[0]["result"], "PIN_OTHER_REPO")
        # Negative control: with no stage allowlists the foreign file is a stop.
        self.plan([self.entry("share_group", "src/tools/share-group.ts")])
        p = self.guard()
        self.assertIn("PIN_UNRESOLVED symbol=share_group", p.stdout)
        self.assertEqual(p.returncode, 1)

    def test_a_stage_pool_is_read_only_for_its_own_repository(self):
        stages = {"api": {"files_allowlist": [self.ctl], "pinned_decisions": [self.entry("GroupController.getGroup", self.ctl)]},
                  "goodword-mcp": {"files_allowlist": [], "pinned_decisions": [self.entry("share_group", "src/tools/x.ts")]}}
        self.plan([], stages=stages)
        p = self.guard()
        self.assertIn("PIN_OK pins=1", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main()
