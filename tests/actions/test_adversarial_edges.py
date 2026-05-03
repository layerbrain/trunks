from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from trunks.actions.artifacts import collect_artifacts
from trunks.actions.backend_store import run_index_by_status_prefix, run_state_ref, store
from trunks.actions.capacity import set_capacity_limit
from trunks.actions.run import enqueue_command, run_command
from trunks.actions.secrets import bind_secret
from trunks.actions.storage import claim_run, list_runs, repair_indexes
from trunks.actions.executor import execute_once
from trunks.actions.workflow import WorkflowError, lint_workflows, parse_workflow
from trunks.cli import dispatch
from trunks.objects import Blob
from trunks.repository import Repository
from trunks.sandboxes import GPU, Spec


class SpecValidationAdversarialTests(unittest.IsolatedAsyncioTestCase):
    def test_impossible_specs_fail_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            Spec(cpu=0, memory_gib=1, disk_gib=1)
        with self.assertRaises(ValueError):
            Spec(cpu=1, memory_gib=0, disk_gib=1)
        with self.assertRaises(ValueError):
            Spec(cpu=1, memory_gib=1, disk_gib=0)
        with self.assertRaises(ValueError):
            Spec(cpu=1, memory_gib=1, disk_gib=1, arch="sparc")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Spec(cpu=1, memory_gib=1, disk_gib=1, network="open")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            GPU(kind="h100", count=0)

    async def test_cli_rejects_zero_sized_specs_before_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                    await dispatch(["actions", "run", "--command", "true", "--cpu", "0"])
                self.assertNotEqual(raised.exception.code, 0)
                self.assertIn("must be >= 1", err.getvalue())
            finally:
                os.chdir(cwd)


class ArtifactAdversarialTests(unittest.TestCase):
    def test_artifact_paths_cannot_escape_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("leak", encoding="utf-8")
            repo = Repository.init(cwd=root, name="demo")
            with self.assertRaises(ValueError):
                collect_artifacts(repo, "run", root=root, paths=("../outside.txt",))

    def test_artifact_symlinks_cannot_escape_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("leak", encoding="utf-8")
            link = root / "leak.txt"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks are not available")
            repo = Repository.init(cwd=root, name="demo")
            with self.assertRaises(ValueError):
                collect_artifacts(repo, "run", root=root, paths=("leak.txt",))

    def test_artifact_hierarchy_survives_resolved_temp_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            artifact = root / "dist" / "deep" / "result.txt"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("payload", encoding="utf-8")
            repo = Repository.init(cwd=root, name="demo")
            entries = collect_artifacts(repo, "run", root=root, paths=("dist",))
            self.assertEqual([entry["name"] for entry in entries], ["dist/deep/result.txt"])


class StorageAndExecutorAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_skips_stale_and_corrupt_pending_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            bad = Blob.from_data(b"not-json")
            repo.put_object(bad.id, bad.canonical())
            store(repo).set_ref(f"{run_index_by_status_prefix(repo, 'pending')}00BAD", bad.id)
            pending = enqueue_command(repo, "printf good > result.txt")

            result = await execute_once(repo, executor="edge-executor", cwd=str(root))
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["id"], pending.id)
            self.assertEqual((root / "result.txt").read_text(encoding="utf-8"), "good")

    async def test_executor_skips_unroutable_runs_but_unknown_executor_provider_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            blocked = enqueue_command(repo, "printf blocked > blocked.txt", region="moon")
            runnable = enqueue_command(repo, "printf runnable > runnable.txt")

            result = await execute_once(repo, executor="executor", cwd=str(root))
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["id"], runnable.id)
            self.assertEqual([item["id"] for item in list_runs(repo, status="pending")], [blocked.id])

            with self.assertRaises(KeyError):
                await execute_once(repo, executor="bad-executor", provider_id="missing", cwd=str(root))

    def test_corrupt_run_state_does_not_take_down_indexes_or_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            bad = Blob.from_data(b"not-json")
            repo.put_object(bad.id, bad.canonical())
            store(repo).set_ref(run_state_ref(repo, "evil"), bad.id)
            store(repo).set_ref(f"{run_index_by_status_prefix(repo, 'pending')}evil", bad.id)

            self.assertEqual(list_runs(repo, status="pending"), [])
            self.assertIsNone(
                claim_run(
                    repo,
                    "evil",
                    executor="executor",
                    provider="local",
                    spec_key="cpu1-mem1g-disk1g-arm64-nogpu-default",
                    region="local",
                    requested_region=None,
                )
            )
            self.assertEqual(repair_indexes(repo)["repaired"], 0)

    def test_expired_running_lease_can_be_reclaimed_with_new_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            pending = enqueue_command(repo, "printf retry")
            first = claim_run(
                repo,
                pending.id,
                executor="executor-1",
                provider="local",
                spec_key=pending.spec.key,
                region="local",
                requested_region=None,
                ttl_s=10,
                now_s=100,
            )
            self.assertIsNotNone(first)
            second = claim_run(
                repo,
                pending.id,
                executor="executor-2",
                provider="local",
                spec_key=pending.spec.key,
                region="local",
                requested_region=None,
                ttl_s=10,
                now_s=111,
            )
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(second["state"]["attempt"], 2)
            self.assertEqual(second["state"]["executor"], "executor-2")

    def test_invalid_capacity_and_secret_inputs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            with self.assertRaises(ValueError):
                set_capacity_limit(repo, region="local", spec_key="cpu1", max_concurrent=-1)
            with self.assertRaises(ValueError):
                bind_secret(repo, "BAD-NAME", "env:TOKEN")

    async def test_no_repo_ad_hoc_run_works_but_repo_read_commands_fail_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                out = io.StringIO()
                with redirect_stdout(out):
                    code = await dispatch(["actions", "run", "--command", "printf detached", "--json"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out.getvalue())["logs"][0]["text"], "detached")

                err = io.StringIO()
                with redirect_stderr(err):
                    code = await dispatch(["actions", "list", "--json"])
                self.assertEqual(code, 1)
                self.assertIn("no .trunks", err.getvalue())
            finally:
                os.chdir(cwd)


class WorkflowAdversarialTests(unittest.TestCase):
    def test_lint_reports_invalid_yaml_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".trunks" / "workflows" / "bad.yml"
            path.parent.mkdir(parents=True)
            path.write_text("name: [unterminated\n", encoding="utf-8")
            result = lint_workflows(root)
            self.assertFalse(result[0]["valid"])
            self.assertIn("invalid YAML", result[0]["error"])

    def test_needs_cycle_and_missing_needs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".trunks" / "workflows" / "cycle.yml"
            path.parent.mkdir(parents=True)
            path.write_text(
                """
name: Cycle
on: push
jobs:
  a:
    needs: b
    steps:
      - run: echo a
  b:
    needs: a
    steps:
      - run: echo b
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(WorkflowError):
                parse_workflow(path, root=root)

            path.write_text(
                """
name: Missing
on: push
jobs:
  a:
    needs: b
    steps:
      - run: echo a
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(WorkflowError):
                parse_workflow(path, root=root)

    def test_invalid_env_names_and_specs_fail_at_parse_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".trunks" / "workflows" / "bad.yml"
            path.parent.mkdir(parents=True)
            path.write_text(
                """
name: BadEnv
on: push
jobs:
  test:
    steps:
      - run: echo bad
        env:
          BAD-NAME: value
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(WorkflowError):
                parse_workflow(path, root=root)

            path.write_text(
                """
name: BadSpec
on: push
jobs:
  test:
    trunks:
      spec:
        cpu: 0
    steps:
      - run: echo bad
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(WorkflowError):
                parse_workflow(path, root=root)

    def test_matrix_explosion_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".trunks" / "workflows" / "huge.yml"
            path.parent.mkdir(parents=True)
            path.write_text(
                """
name: Huge
on: push
jobs:
  test:
    strategy:
      matrix:
        a: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
        b: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    steps:
      - run: echo too many
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(WorkflowError):
                parse_workflow(path, root=root)


class RunLifecycleAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonzero_exit_and_timeout_are_failed_runs(self) -> None:
        nonzero = await run_command("printf fail; exit 7")
        self.assertEqual(nonzero.state.phase, "failed")
        self.assertEqual(nonzero.result.exit_code if nonzero.result else None, 7)

        timed_out = await run_command("sleep 2", timeout_s=1)
        self.assertEqual(timed_out.state.phase, "failed")
        self.assertTrue(timed_out.result.timed_out if timed_out.result else False)

    async def test_large_log_stream_preserves_all_lines(self) -> None:
        run = await run_command("i=1; while [ $i -le 200 ]; do echo line-$i; i=$((i+1)); done")
        self.assertEqual(run.state.phase, "succeeded")
        self.assertEqual(len(run.logs), 200)
        self.assertEqual(run.logs[0].text, "line-1\n")
        self.assertEqual(run.logs[-1].text, "line-200\n")


if __name__ == "__main__":
    unittest.main()
