from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from trunks.actions.artifacts import list_artifacts
from trunks.actions.workflow import WorkflowError, lint_workflows, parse_workflow
from trunks.actions.workflow import workflow_run_jobs
from trunks.cli import dispatch
from trunks.repository import Repository


class WorkflowYmlTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_run_executes_needs_order_and_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_dir = root / ".trunks" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "ci.yml").write_text(
                """
name: CI
on: [push, workflow_dispatch]
jobs:
  prepare:
    steps:
      - uses: actions/checkout@v4
      - run: printf "${{ github.sha }} " > sha.txt
      - run: printf "$STEP_VALUE" >> sha.txt
        env:
          STEP_VALUE: step-env
      - run: printf prepare > order.txt
  test:
    needs: prepare
    strategy:
      matrix:
        py: ["3.12", "3.13"]
    steps:
      - run: printf " ${{ matrix.py }}" >> order.txt
      - run: mkdir -p reports && printf "${{ matrix.py }}" > reports/${{ matrix.py }}.txt
      - uses: actions/upload-artifact@v4
        with:
          path: reports/${{ matrix.py }}.txt
""".strip(),
                encoding="utf-8",
            )
            cwd = Path.cwd()
            os.chdir(root)
            try:
                Repository.init(name="demo")
                out = io.StringIO()
                with redirect_stdout(out):
                    code = await dispatch(["actions", "run", "--json"])
                self.assertEqual(code, 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["_schema_version"], "trunks.actions.workflow_run.v1")
                self.assertEqual(payload["phase"], "succeeded")
                workflow_run_id = payload["id"]
                self.assertEqual([job["job"] for job in payload["jobs"]], ["prepare", "test", "test"])
                self.assertEqual((root / "order.txt").read_text(encoding="utf-8"), "prepare 3.12 3.13")
                self.assertEqual((root / "sha.txt").read_text(encoding="utf-8"), "worktree step-env")
                self.assertEqual(payload["jobs"][1]["phase"], "succeeded")

                jobs_out = io.StringIO()
                with redirect_stdout(jobs_out):
                    code = await dispatch(["actions", "workflow-runs", "--id", workflow_run_id, "jobs", "--json"])
                self.assertEqual(code, 0)
                jobs = json.loads(jobs_out.getvalue())
                self.assertEqual([job["job"] for job in jobs["data"]], ["prepare", "test", "test"])

                graph_out = io.StringIO()
                with redirect_stdout(graph_out):
                    code = await dispatch(["actions", "workflow-runs", "--id", workflow_run_id, "graph", "--json"])
                self.assertEqual(code, 0)
                graph = json.loads(graph_out.getvalue())
                self.assertEqual(graph["data"], [{"from": "prepare", "to": "test"}])

                show_out = io.StringIO()
                with redirect_stdout(show_out):
                    code = await dispatch(["actions", "workflows", "show", "CI", "--json"])
                self.assertEqual(code, 0)
                shown = json.loads(show_out.getvalue())
                self.assertEqual(shown["name"], "CI")
            finally:
                os.chdir(cwd)

    async def test_upload_artifact_multiline_path_collects_each_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_dir = root / ".trunks" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "ci.yml").write_text(
                """
name: CI
on: [workflow_dispatch]
jobs:
  test:
    steps:
      - run: mkdir -p logs && printf report > report.json && printf log > logs/app.log
      - uses: actions/upload-artifact@v4
        with:
          path: |
            report.json
            logs/app.log
""".strip(),
                encoding="utf-8",
            )
            cwd = Path.cwd()
            os.chdir(root)
            try:
                repo = Repository.init(name="demo")
                out = io.StringIO()
                with redirect_stdout(out):
                    code = await dispatch(["actions", "run", "--json"])
                self.assertEqual(code, 0)
                payload = json.loads(out.getvalue())
                job_run_id = workflow_run_jobs(repo, payload["id"])[0]["run"]
                self.assertEqual(
                    [item["name"] for item in list_artifacts(repo, job_run_id)],
                    ["logs/app.log", "report.json"],
                )
            finally:
                os.chdir(cwd)

    async def test_workflows_lint_reports_strict_mode_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_dir = root / ".trunks" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "ci.yml").write_text(
                """
name: CI
on: push
jobs:
  test:
    environment: production
    steps:
      - run: echo no
""".strip(),
                encoding="utf-8",
            )
            Repository.init(cwd=root, name="demo")
            cwd = Path.cwd()
            os.chdir(root)
            try:
                out = io.StringIO()
                with redirect_stdout(out):
                    code = await dispatch(["actions", "workflows", "lint", "--json"])
                self.assertEqual(code, 0)
                payload = json.loads(out.getvalue())
                self.assertFalse(payload["data"][0]["valid"])
                self.assertIn("environment", payload["data"][0]["error"])
            finally:
                os.chdir(cwd)

    def test_parser_keeps_on_as_string_and_rejects_id_token_without_oidc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".trunks" / "workflows" / "deploy.yml"
            path.parent.mkdir(parents=True)
            path.write_text(
                """
name: Deploy
on:
  workflow_dispatch:
permissions:
  id-token: write
jobs:
  deploy:
    steps:
      - run: echo deploy
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(WorkflowError):
                parse_workflow(path, root=root)
            workflow = parse_workflow(path, root=root, oidc_enabled=True)
            self.assertIn("workflow_dispatch", workflow.triggers)

    def test_unsupported_uses_fail_closed_unless_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".trunks" / "workflows" / "ci.yml"
            path.parent.mkdir(parents=True)
            path.write_text(
                """
name: CI
on: push
jobs:
  test:
    steps:
      - uses: docker/build-push-action@v6
""".strip(),
                encoding="utf-8",
            )
            strict = lint_workflows(root)
            self.assertFalse(strict[0]["valid"])
            relaxed = lint_workflows(root, accept_best_effort=True)
            self.assertTrue(relaxed[0]["valid"])

    async def test_migrate_copies_github_workflow_to_trunks_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = root / ".github" / "workflows"
            github.mkdir(parents=True)
            source = github / "ci.yml"
            source.write_text(
                """
name: CI
on: push
jobs:
  test:
    steps:
      - run: echo migrated
""".strip(),
                encoding="utf-8",
            )
            cwd = Path.cwd()
            os.chdir(root)
            try:
                out = io.StringIO()
                with redirect_stdout(out):
                    code = await dispatch(["actions", "migrate", str(source), "--json"])
                self.assertEqual(code, 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["_schema_version"], "trunks.actions.workflow_migration.v1")
                self.assertEqual((root / ".trunks" / "workflows" / "ci.yml").read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
