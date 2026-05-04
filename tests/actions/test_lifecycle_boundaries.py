from __future__ import annotations

import tempfile
import unittest

from trunks.actions.run import enqueue_command
from trunks.actions.backend_store import run_state_ref, store
from trunks.actions.workflow import list_workflow_runs
from trunks.backends.memory import Memory
from trunks.engine import Engine
from trunks.repository import Repository


class ActionsLifecycleBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_branch_push_does_not_publish_actions_refs(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(cwd=tmp, name="demo")
            engine = Engine(repository=repo, backend=backend)
            await engine.write("README.md", b"# demo\n")
            commit = await engine.commit(message="init")
            action = enqueue_command(repo, "printf lifecycle")

            self.assertIsNotNone(store(repo).read_ref(run_state_ref(repo, action.id)))
            await engine.push()

            self.assertEqual(await backend.read_ref("main"), commit.id)
            action_refs = [ref.name async for ref in backend.list_refs("refs/actions")]
            self.assertEqual(action_refs, [])

    async def test_branch_push_triggers_trunks_workflows_with_push_trigger(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(cwd=tmp, name="demo")
            root = repo.root
            workflows = root / ".trunks" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "ci.yml").write_text(
                """
name: CI
on: [push]
jobs:
  test:
    steps:
      - run: printf pushed
""".strip(),
                encoding="utf-8",
            )
            repo.add_worktree_path(root)
            commit = repo.create_commit(message="add workflow")
            engine = Engine(repository=repo, backend=backend)

            await engine.push()

            workflow_runs = list_workflow_runs(repo)
            self.assertEqual(len(workflow_runs), 1)
            self.assertEqual(workflow_runs[0]["phase"], "pending")
            self.assertEqual(workflow_runs[0]["commit"], str(commit.id))
            self.assertEqual(workflow_runs[0]["jobs"][0]["phase"], "pending")


if __name__ == "__main__":
    unittest.main()
