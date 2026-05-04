from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trunks.actions.workflow import (
    TriggerFilter,
    _parse_triggers,
    pull_request_trigger_matches,
    push_trigger_matches,
    trigger_matches_branch,
    trigger_matches_paths,
    trigger_matches_tags,
    trigger_matches_types,
    parse_workflow,
)


class ParseTriggerTests(unittest.TestCase):
    def test_on_string(self) -> None:
        result = _parse_triggers("push")
        self.assertEqual(set(result), {"push"})
        self.assertIsInstance(result["push"], TriggerFilter)
        self.assertEqual(result["push"].branches, ())

    def test_on_list(self) -> None:
        result = _parse_triggers(["push", "workflow_dispatch"])
        self.assertEqual(set(result), {"push", "workflow_dispatch"})

    def test_on_dict_no_filters(self) -> None:
        result = _parse_triggers({"push": None, "pull_request": None})
        self.assertEqual(set(result), {"push", "pull_request"})
        self.assertEqual(result["push"].branches, ())

    def test_on_dict_with_branch_filter(self) -> None:
        result = _parse_triggers({"push": {"branches": ["main", "release/*"]}})
        self.assertEqual(result["push"].branches, ("main", "release/*"))
        self.assertEqual(result["push"].branches_ignore, ())

    def test_on_dict_with_branches_ignore(self) -> None:
        result = _parse_triggers({"push": {"branches-ignore": ["tmp/*"]}})
        self.assertEqual(result["push"].branches_ignore, ("tmp/*",))

    def test_on_dict_with_paths(self) -> None:
        result = _parse_triggers({"push": {"paths": ["src/**", "*.py"]}})
        self.assertEqual(result["push"].paths, ("src/**", "*.py"))

    def test_on_dict_with_paths_ignore(self) -> None:
        result = _parse_triggers({"push": {"paths-ignore": ["docs/**"]}})
        self.assertEqual(result["push"].paths_ignore, ("docs/**",))

    def test_on_dict_with_tags(self) -> None:
        result = _parse_triggers({"push": {"tags": ["v*"]}})
        self.assertEqual(result["push"].tags, ("v*",))

    def test_pull_request_with_types(self) -> None:
        result = _parse_triggers({"pull_request": {"types": ["opened", "synchronize"], "branches": ["main"]}})
        self.assertEqual(result["pull_request"].types, ("opened", "synchronize"))
        self.assertEqual(result["pull_request"].branches, ("main",))

    def test_on_none(self) -> None:
        self.assertEqual(_parse_triggers(None), {})

    def test_mixed_triggers(self) -> None:
        result = _parse_triggers({
            "push": {"branches": ["main"]},
            "pull_request": {"branches": ["main"], "types": ["opened"]},
            "workflow_dispatch": None,
        })
        self.assertEqual(set(result), {"push", "pull_request", "workflow_dispatch"})
        self.assertEqual(result["push"].branches, ("main",))
        self.assertEqual(result["pull_request"].types, ("opened",))
        self.assertEqual(result["workflow_dispatch"].branches, ())


class BranchMatchTests(unittest.TestCase):
    def test_no_filter_matches_all(self) -> None:
        filt = TriggerFilter()
        self.assertTrue(trigger_matches_branch(filt, "main"))
        self.assertTrue(trigger_matches_branch(filt, "feature/xyz"))

    def test_branches_include(self) -> None:
        filt = TriggerFilter(branches=("main", "release/*"))
        self.assertTrue(trigger_matches_branch(filt, "main"))
        self.assertTrue(trigger_matches_branch(filt, "release/v1"))
        self.assertFalse(trigger_matches_branch(filt, "feature/xyz"))
        self.assertFalse(trigger_matches_branch(filt, "develop"))

    def test_branches_ignore(self) -> None:
        filt = TriggerFilter(branches_ignore=("tmp/*", "wip/*"))
        self.assertTrue(trigger_matches_branch(filt, "main"))
        self.assertTrue(trigger_matches_branch(filt, "feature/xyz"))
        self.assertFalse(trigger_matches_branch(filt, "tmp/test"))
        self.assertFalse(trigger_matches_branch(filt, "wip/draft"))

    def test_both_branches_and_ignore_rejects(self) -> None:
        filt = TriggerFilter(branches=("main",), branches_ignore=("develop",))
        self.assertFalse(trigger_matches_branch(filt, "main"))


class PathMatchTests(unittest.TestCase):
    def test_no_filter_matches_all(self) -> None:
        filt = TriggerFilter()
        self.assertTrue(trigger_matches_paths(filt, ["anything.py"]))
        self.assertTrue(trigger_matches_paths(filt, []))

    def test_paths_include(self) -> None:
        filt = TriggerFilter(paths=("src/*", "*.py"))
        self.assertTrue(trigger_matches_paths(filt, ["src/main.py"]))
        self.assertTrue(trigger_matches_paths(filt, ["utils.py"]))
        self.assertFalse(trigger_matches_paths(filt, ["docs/readme.md"]))
        self.assertFalse(trigger_matches_paths(filt, ["README.md"]))

    def test_paths_ignore(self) -> None:
        filt = TriggerFilter(paths_ignore=("docs/*", "*.md"))
        self.assertTrue(trigger_matches_paths(filt, ["src/main.py"]))
        self.assertTrue(trigger_matches_paths(filt, ["src/main.py", "docs/readme.md"]))
        self.assertFalse(trigger_matches_paths(filt, ["docs/readme.md", "CHANGELOG.md"]))

    def test_empty_changed_files_with_paths_filter(self) -> None:
        filt = TriggerFilter(paths=("src/*",))
        self.assertFalse(trigger_matches_paths(filt, []))


class TagMatchTests(unittest.TestCase):
    def test_no_tag_no_filter(self) -> None:
        self.assertTrue(trigger_matches_tags(TriggerFilter(), None))

    def test_tag_filter_no_tag_event(self) -> None:
        filt = TriggerFilter(tags=("v*",))
        self.assertFalse(trigger_matches_tags(filt, None))

    def test_tag_matches(self) -> None:
        filt = TriggerFilter(tags=("v*",))
        self.assertTrue(trigger_matches_tags(filt, "v1.0"))
        self.assertFalse(trigger_matches_tags(filt, "release-1.0"))

    def test_tags_ignore(self) -> None:
        filt = TriggerFilter(tags_ignore=("v*-rc*",))
        self.assertTrue(trigger_matches_tags(filt, "v1.0"))
        self.assertFalse(trigger_matches_tags(filt, "v1.0-rc1"))


class TypeMatchTests(unittest.TestCase):
    def test_no_types_matches_all(self) -> None:
        filt = TriggerFilter()
        self.assertTrue(trigger_matches_types(filt, "opened"))
        self.assertTrue(trigger_matches_types(filt, "synchronize"))

    def test_types_filter(self) -> None:
        filt = TriggerFilter(types=("opened", "synchronize"))
        self.assertTrue(trigger_matches_types(filt, "opened"))
        self.assertTrue(trigger_matches_types(filt, "synchronize"))
        self.assertFalse(trigger_matches_types(filt, "closed"))


class PushTriggerMatchesTests(unittest.TestCase):
    def test_unfiltered_push_matches_everything(self) -> None:
        filt = TriggerFilter()
        self.assertTrue(push_trigger_matches(filt, branch="main", changed_files=["a.py"]))
        self.assertTrue(push_trigger_matches(filt, branch="feature/x", changed_files=["docs/x.md"]))

    def test_branch_filter_gates(self) -> None:
        filt = TriggerFilter(branches=("main",))
        self.assertTrue(push_trigger_matches(filt, branch="main", changed_files=["a.py"]))
        self.assertFalse(push_trigger_matches(filt, branch="develop", changed_files=["a.py"]))

    def test_path_filter_gates(self) -> None:
        filt = TriggerFilter(paths=("src/*",))
        self.assertTrue(push_trigger_matches(filt, branch="main", changed_files=["src/x.py"]))
        self.assertFalse(push_trigger_matches(filt, branch="main", changed_files=["docs/x.md"]))

    def test_branch_and_path_both_must_match(self) -> None:
        filt = TriggerFilter(branches=("main",), paths=("src/*",))
        self.assertTrue(push_trigger_matches(filt, branch="main", changed_files=["src/x.py"]))
        self.assertFalse(push_trigger_matches(filt, branch="main", changed_files=["docs/x.md"]))
        self.assertFalse(push_trigger_matches(filt, branch="develop", changed_files=["src/x.py"]))

    def test_tag_push(self) -> None:
        filt = TriggerFilter(tags=("v*",))
        self.assertTrue(push_trigger_matches(filt, branch="main", changed_files=[], tag="v1.0"))
        self.assertFalse(push_trigger_matches(filt, branch="main", changed_files=[], tag=None))


class PullRequestTriggerMatchesTests(unittest.TestCase):
    def test_unfiltered_pr_matches_default_type(self) -> None:
        filt = TriggerFilter()
        self.assertTrue(pull_request_trigger_matches(filt, branch="main", changed_files=["a.py"]))

    def test_pr_branch_filter(self) -> None:
        filt = TriggerFilter(branches=("main",))
        self.assertTrue(pull_request_trigger_matches(filt, branch="main", changed_files=["a.py"]))
        self.assertFalse(pull_request_trigger_matches(filt, branch="develop", changed_files=["a.py"]))

    def test_pr_type_filter(self) -> None:
        filt = TriggerFilter(types=("opened",))
        self.assertTrue(pull_request_trigger_matches(filt, branch="main", changed_files=["a.py"], event_type="opened"))
        self.assertFalse(pull_request_trigger_matches(filt, branch="main", changed_files=["a.py"], event_type="closed"))

    def test_pr_paths_filter(self) -> None:
        filt = TriggerFilter(paths=("src/*",))
        self.assertTrue(pull_request_trigger_matches(filt, branch="main", changed_files=["src/x.py"]))
        self.assertFalse(pull_request_trigger_matches(filt, branch="main", changed_files=["docs/x.md"]))

    def test_pr_branch_and_type_and_paths(self) -> None:
        filt = TriggerFilter(branches=("main",), types=("opened", "synchronize"), paths=("src/*",))
        self.assertTrue(pull_request_trigger_matches(filt, branch="main", changed_files=["src/x.py"], event_type="opened"))
        self.assertFalse(pull_request_trigger_matches(filt, branch="main", changed_files=["src/x.py"], event_type="closed"))
        self.assertFalse(pull_request_trigger_matches(filt, branch="develop", changed_files=["src/x.py"], event_type="opened"))
        self.assertFalse(pull_request_trigger_matches(filt, branch="main", changed_files=["docs/x.md"], event_type="opened"))


class ParseWorkflowTriggerIntegrationTests(unittest.TestCase):
    def test_full_push_filter_parses_from_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".trunks" / "workflows" / "ci.yml"
            path.parent.mkdir(parents=True)
            path.write_text("""
name: CI
on:
  push:
    branches: [main, release/*]
    paths: [src/**, tests/**]
  pull_request:
    branches: [main]
    types: [opened, synchronize]
    paths-ignore: [docs/**]
jobs:
  test:
    steps:
      - run: echo test
""".strip(), encoding="utf-8")
            workflow = parse_workflow(path, root=root)
            self.assertIn("push", workflow.triggers)
            self.assertIn("pull_request", workflow.triggers)
            push_filt = workflow.triggers["push"]
            self.assertEqual(push_filt.branches, ("main", "release/*"))
            self.assertEqual(push_filt.paths, ("src/**", "tests/**"))
            pr_filt = workflow.triggers["pull_request"]
            self.assertEqual(pr_filt.branches, ("main",))
            self.assertEqual(pr_filt.types, ("opened", "synchronize"))
            self.assertEqual(pr_filt.paths_ignore, ("docs/**",))

    def test_simple_on_push_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".trunks" / "workflows" / "ci.yml"
            path.parent.mkdir(parents=True)
            path.write_text("""
name: CI
on: push
jobs:
  test:
    steps:
      - run: echo test
""".strip(), encoding="utf-8")
            workflow = parse_workflow(path, root=root)
            self.assertIn("push", workflow.triggers)
            filt = workflow.triggers["push"]
            self.assertEqual(filt.branches, ())
            self.assertEqual(filt.paths, ())

    def test_on_list_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".trunks" / "workflows" / "ci.yml"
            path.parent.mkdir(parents=True)
            path.write_text("""
name: CI
on: [push, workflow_dispatch]
jobs:
  test:
    steps:
      - run: echo test
""".strip(), encoding="utf-8")
            workflow = parse_workflow(path, root=root)
            self.assertIn("push", workflow.triggers)
            self.assertIn("workflow_dispatch", workflow.triggers)
            self.assertEqual(list(workflow.to_dict()["triggers"]), ["push", "workflow_dispatch"])


if __name__ == "__main__":
    unittest.main()
