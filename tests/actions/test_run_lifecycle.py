from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from trunks.actions.capacity import capacity_snapshot, set_capacity_limit
from trunks.actions.backend_store import run_index_by_status_prefix, run_state_ref, store
from trunks.actions.run import enqueue_command, run_command
from trunks.actions.storage import cancel_run, claim_run, complete_run, heartbeat_run, list_runs, load_run, prune_runs, repair_indexes
from trunks.actions.watch import watch_run_async
from trunks.actions.executor import execute_once
from trunks.cli import dispatch
from trunks.repository import Repository


class ActionSyncApiTests(unittest.TestCase):
    def test_run_command_sync_outside_event_loop(self) -> None:
        run = run_command("printf 'sync\\n'")
        self.assertFalse(asyncio.iscoroutine(run))
        self.assertEqual(run.to_dict()["logs"][0]["text"], "sync\n")

    def test_action_execute_sync_outside_event_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            pending = enqueue_command(repo, "printf sync-executor > action-result.txt")
            result = execute_once(repo, executor="sync-executor", cwd=str(root))
            self.assertFalse(asyncio.iscoroutine(result))
            assert isinstance(result, dict)
            self.assertEqual(result["id"], pending.id)
            self.assertEqual((root / "action-result.txt").read_text(encoding="utf-8"), "sync-executor")


class ActionRunLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_command_returns_versioned_payload(self) -> None:
        run = await run_command("printf 'hello\\n'")
        payload = run.to_dict()
        self.assertEqual(payload["_schema_version"], "trunks.actions.run.v1")
        self.assertEqual(payload["state"]["_schema_version"], "trunks.actions.run_state.v1")
        self.assertEqual(payload["state"]["phase"], "succeeded")
        self.assertEqual(payload["state"]["provider"], "local")
        self.assertEqual(payload["result"]["exit_code"], 0)
        self.assertEqual(payload["logs"][0]["text"], "hello\n")

    async def test_cli_actions_run_is_lazy_and_json_encoded(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = await dispatch(["actions", "run", "--command", "printf 'hi\\n'", "--cpu", "1", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["_schema_version"], "trunks.actions.run.v1")
        self.assertEqual(payload["spec"]["cpu"], 1)
        self.assertEqual(payload["state"]["provider"], "local")
        self.assertEqual(payload["logs"][0]["seq"], 1)

    async def test_cli_sandboxes_lists_local_provider(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = await dispatch(["sandboxes", "providers", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        providers = {item["id"]: item for item in payload["data"]}
        self.assertIn("local", providers)
        self.assertEqual(providers["local"]["_schema_version"], "trunks.sandboxes.provider_info.v1")

    async def test_run_persists_to_trunks_refs_when_inside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                repo = Repository.init(name="demo")
                run = await run_command("printf 'persist\\n'", cwd=str(root))
                state_ref = store(repo).read_ref(run_state_ref(repo, run.id))
                self.assertIsNotNone(state_ref)
                loaded = load_run(repo, run.id)
                self.assertEqual(loaded["_schema_version"], "trunks.actions.run.v1")
                self.assertEqual(loaded["state"]["phase"], "succeeded")
                self.assertEqual(store(repo).read_ref(f"{run_index_by_status_prefix(repo, 'succeeded')}{run.id}"), state_ref)
                listed = list_runs(repo)
                self.assertEqual([item["id"] for item in listed], [run.id])
            finally:
                os.chdir(cwd)

    async def test_claim_and_complete_run_are_fenced_by_cas_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            pending = enqueue_command(repo, "printf 'queued\\n'")

            claimed = claim_run(
                repo,
                pending.id,
                executor="executor-1",
                provider="local",
                spec_key=pending.spec.key,
                region="local",
                requested_region=None,
                now_s=100,
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(list_runs(repo, status="pending"), [])
            self.assertEqual([item["id"] for item in list_runs(repo, status="running")], [pending.id])

            assert claimed is not None
            state = claimed["state"]
            self.assertIsInstance(state, dict)
            assert isinstance(state, dict)
            token = state["token"]
            self.assertIsInstance(token, str)
            self.assertFalse(heartbeat_run(repo, pending.id, token="wrong", now_s=110))
            self.assertTrue(heartbeat_run(repo, pending.id, token=str(token), ttl_s=90, now_s=110))
            self.assertEqual(load_run(repo, pending.id)["state"]["expires_at"], 200)
            self.assertFalse(complete_run(repo, claimed, token="wrong", phase="succeeded", logs=[], result=None))
            self.assertTrue(
                complete_run(
                    repo,
                    claimed,
                    token=str(token),
                    phase="succeeded",
                    logs=[{"stream": "stdout", "seq": 1, "text": "queued\n", "ts_ms": 1000}],
                    result={"exit_code": 0, "duration_ms": 5, "timed_out": False},
                )
            )
            loaded = load_run(repo, pending.id)
            self.assertEqual(loaded["state"]["phase"], "succeeded")
            self.assertEqual(list_runs(repo, status="running"), [])
            self.assertEqual([item["id"] for item in list_runs(repo, status="succeeded")], [pending.id])

    async def test_cli_reads_persisted_runs_from_index_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                Repository.init(name="demo")
                run_out = io.StringIO()
                with redirect_stdout(run_out):
                    code = await dispatch(["actions", "run", "--command", "printf 'from-index\\n'", "--json"])
                self.assertEqual(code, 0)
                run_id = json.loads(run_out.getvalue())["id"]

                list_out = io.StringIO()
                with redirect_stdout(list_out):
                    code = await dispatch(["actions", "list", "--json"])
                self.assertEqual(code, 0)
                listed = json.loads(list_out.getvalue())
                self.assertEqual(listed["_schema_version"], "trunks.actions.run_list.v1")
                self.assertEqual([item["id"] for item in listed["data"]], [run_id])

                status_out = io.StringIO()
                with redirect_stdout(status_out):
                    code = await dispatch(["actions", "status", "--id", run_id, "--json"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(status_out.getvalue())["phase"], "succeeded")

                describe_out = io.StringIO()
                with redirect_stdout(describe_out):
                    code = await dispatch(["actions", "describe", "--id", run_id, "--json"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(describe_out.getvalue())["id"], run_id)

                logs_out = io.StringIO()
                with redirect_stdout(logs_out):
                    code = await dispatch(["actions", "logs", "--id", run_id])
                self.assertEqual(code, 0)
                self.assertEqual(logs_out.getvalue(), "from-index\n")
            finally:
                os.chdir(cwd)

    async def test_cli_run_stores_and_downloads_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                Repository.init(name="demo")
                run_out = io.StringIO()
                with redirect_stdout(run_out):
                    code = await dispatch(
                        [
                            "actions",
                            "run",
                            "--command",
                            "mkdir -p dist && printf artifact > dist/result.txt",
                            "--artifact",
                            "dist",
                            "--json",
                        ]
                    )
                self.assertEqual(code, 0)
                run_id = json.loads(run_out.getvalue())["id"]

                list_out = io.StringIO()
                with redirect_stdout(list_out):
                    code = await dispatch(["actions", "artifacts", "--id", run_id, "--json"])
                self.assertEqual(code, 0)
                artifacts = json.loads(list_out.getvalue())
                self.assertEqual(artifacts["data"][0]["name"], "dist/result.txt")

                target = root / "download.txt"
                code = await dispatch(["actions", "artifacts", "--id", run_id, "get", "dist/result.txt", "-o", str(target)])
                self.assertEqual(code, 0)
                self.assertEqual(target.read_text(encoding="utf-8"), "artifact")
            finally:
                os.chdir(cwd)

    async def test_secret_bindings_store_metadata_and_inject_env_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            old_secret = os.environ.get("TRUNKS_TEST_TOKEN")
            token_value = f"token-value-{root.name}"
            os.environ["TRUNKS_TEST_TOKEN"] = token_value
            os.chdir(root)
            try:
                repo = Repository.init(name="demo")
                bind_out = io.StringIO()
                with redirect_stdout(bind_out):
                    code = await dispatch(["actions", "secrets", "bind", "NPM_TOKEN", "env:TRUNKS_TEST_TOKEN", "--json"])
                self.assertEqual(code, 0)
                binding = json.loads(bind_out.getvalue())
                self.assertEqual(binding["source_type"], "env")
                self.assertNotIn("source", binding)
                self.assertNotIn(token_value, bind_out.getvalue())

                run_out = io.StringIO()
                with redirect_stdout(run_out):
                    code = await dispatch(["actions", "run", "--command", "printf \"$NPM_TOKEN\"", "--json"])
                self.assertEqual(code, 0)
                payload = json.loads(run_out.getvalue())
                self.assertEqual(payload["logs"][0]["text"], "***")
                self.assertNotIn(token_value, run_out.getvalue())

                queued = enqueue_command(repo, "printf \"$NPM_TOKEN\"")
                queued_result = await execute_once(repo, executor="secret-executor", cwd=str(root))
                self.assertIsNotNone(queued_result)
                assert queued_result is not None
                self.assertEqual(queued_result["id"], queued.id)
                self.assertEqual(queued_result["logs"][0]["text"], "***")
                self.assertNotIn(token_value, json.dumps(queued_result))

                list_out = io.StringIO()
                with redirect_stdout(list_out):
                    code = await dispatch(["actions", "secrets", "list", "--json"])
                self.assertEqual(code, 0)
                bindings = json.loads(list_out.getvalue())
                self.assertEqual(bindings["data"][0]["name"], "NPM_TOKEN")
                self.assertEqual(bindings["data"][0]["source_type"], "env")
                self.assertNotIn("source", bindings["data"][0])
                self.assertNotIn(token_value, list_out.getvalue())
            finally:
                if old_secret is None:
                    os.environ.pop("TRUNKS_TEST_TOKEN", None)
                else:
                    os.environ["TRUNKS_TEST_TOKEN"] = old_secret
                os.chdir(cwd)

    async def test_cli_enqueue_and_action_execute_complete_pending_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                Repository.init(name="demo")
                enqueue_out = io.StringIO()
                with redirect_stdout(enqueue_out):
                    code = await dispatch(["actions", "enqueue", "--command", "printf 'queued-cli\\n'", "--json"])
                self.assertEqual(code, 0)
                run_id = json.loads(enqueue_out.getvalue())["id"]

                executor_out = io.StringIO()
                with redirect_stdout(executor_out):
                    code = await dispatch(["actions", "execute", "--id", "executor-1", "--json"])
                self.assertEqual(code, 0)
                executor_payload = json.loads(executor_out.getvalue())
                self.assertEqual(executor_payload["_schema_version"], "trunks.actions.execute.v1")
                self.assertEqual(executor_payload["run"]["id"], run_id)
                self.assertEqual(executor_payload["run"]["state"]["phase"], "succeeded")

                logs_out = io.StringIO()
                with redirect_stdout(logs_out):
                    code = await dispatch(["actions", "logs", "--id", run_id])
                self.assertEqual(code, 0)
                self.assertEqual(logs_out.getvalue(), "queued-cli\n")
            finally:
                os.chdir(cwd)

    async def test_cli_enqueue_preserves_execution_options_for_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                Repository.init(name="demo")
                enqueue_out = io.StringIO()
                with redirect_stdout(enqueue_out):
                    code = await dispatch(
                        [
                            "actions",
                            "enqueue",
                            "--command",
                            "mkdir -p queued-artifacts && printf queued > queued-artifacts/result.txt",
                            "--provider",
                            "local",
                            "--strict-provider",
                            "--timeout",
                            "5",
                            "--artifact",
                            "queued-artifacts",
                            "--json",
                        ]
                    )
                self.assertEqual(code, 0)
                queued = json.loads(enqueue_out.getvalue())
                self.assertEqual(queued["provider_id"], "local")
                self.assertTrue(queued["strict_provider"])
                self.assertEqual(queued["timeout_s"], 5)
                self.assertEqual(queued["artifact_paths"], ["queued-artifacts"])

                execute_out = io.StringIO()
                with redirect_stdout(execute_out):
                    code = await dispatch(["actions", "execute", "--id", "options-executor", "--json"])
                self.assertEqual(code, 0)
                executed = json.loads(execute_out.getvalue())["run"]
                self.assertEqual(executed["id"], queued["id"])
                self.assertEqual(executed["state"]["provider"], "local")
                self.assertEqual(executed["state"]["phase"], "succeeded")
                self.assertEqual(executed["artifacts"][0]["name"], "queued-artifacts/result.txt")
            finally:
                os.chdir(cwd)

    async def test_executors_are_scoped_to_their_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a_root = root / "repo-a"
            repo_b_root = root / "repo-b"
            repo_a_root.mkdir()
            repo_b_root.mkdir()
            repo_a = Repository.init(cwd=repo_a_root, name="alpha")
            repo_b = Repository.init(cwd=repo_b_root, name="beta")
            run_a = enqueue_command(repo_a, "printf alpha > action-result.txt")
            run_b = enqueue_command(repo_b, "printf beta > action-result.txt")

            result_a = await execute_once(repo_a, executor="executor-a", cwd=str(repo_a_root))
            self.assertIsNotNone(result_a)
            assert result_a is not None
            self.assertEqual(result_a["id"], run_a.id)
            self.assertEqual((repo_a_root / "action-result.txt").read_text(encoding="utf-8"), "alpha")
            self.assertFalse((repo_b_root / "action-result.txt").exists())
            self.assertEqual([item["id"] for item in list_runs(repo_b, status="pending")], [run_b.id])

            result_b = await execute_once(repo_b, executor="executor-b", cwd=str(repo_b_root))
            self.assertIsNotNone(result_b)
            assert result_b is not None
            self.assertEqual(result_b["id"], run_b.id)
            self.assertEqual((repo_b_root / "action-result.txt").read_text(encoding="utf-8"), "beta")

    async def test_two_executors_do_not_execute_the_same_run_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            pending = enqueue_command(repo, "sleep 0.1; printf once > action-result.txt")

            results = await asyncio.gather(
                execute_once(repo, executor="executor-1", cwd=str(root)),
                execute_once(repo, executor="executor-2", cwd=str(root)),
            )
            completed = [result for result in results if result is not None]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["id"], pending.id)
            self.assertEqual((root / "action-result.txt").read_text(encoding="utf-8"), "once")
            self.assertEqual([item["id"] for item in list_runs(repo, status="succeeded")], [pending.id])
            self.assertEqual(list_runs(repo, status="pending"), [])

    async def test_watch_streams_executor_logs_while_run_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            pending = enqueue_command(repo, "echo start; sleep 1; echo end")
            executor = asyncio.create_task(execute_once(repo, executor="watch-executor", cwd=str(root)))
            started = asyncio.get_running_loop().time()
            seen_start = False
            async for event in watch_run_async(repo, pending.id, interval_s=0.05, timeout_s=5):
                if event.get("object") == "action_log" and event.get("text") == "start\n":
                    seen_start = True
                    self.assertLess(asyncio.get_running_loop().time() - started, 0.75)
                    break
            self.assertTrue(seen_start)
            completed = await executor
            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed["state"]["phase"], "succeeded")

    async def test_executor_heartbeats_and_cancels_running_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            pending = enqueue_command(repo, "echo started; sleep 5; printf done > done.txt")
            executor = asyncio.create_task(
                execute_once(
                    repo,
                    executor="cancel-executor",
                    cwd=str(root),
                    lease_ttl_s=2,
                    heartbeat_interval_s=0.1,
                )
            )
            for _ in range(50):
                state = load_run(repo, pending.id)["state"]
                if isinstance(state, dict) and state.get("phase") == "running":
                    break
                await asyncio.sleep(0.05)
            initial_expires_at = load_run(repo, pending.id)["state"]["expires_at"]
            await asyncio.sleep(1.2)
            renewed_expires_at = load_run(repo, pending.id)["state"]["expires_at"]
            self.assertGreater(renewed_expires_at, initial_expires_at)

            canceled = cancel_run(repo, pending.id)
            self.assertIsNotNone(canceled)
            result = await asyncio.wait_for(executor, timeout=3)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["state"]["phase"], "canceled")
            self.assertFalse((root / "done.txt").exists())

    async def test_capacity_limit_blocks_additional_claims_for_spec_and_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            first = enqueue_command(repo, "echo first; sleep 5; printf first > first.txt")
            second = enqueue_command(repo, "printf second > second.txt")
            set_capacity_limit(repo, region="local", spec_key=first.spec.key, max_concurrent=1)
            first_executor = asyncio.create_task(
                execute_once(
                    repo,
                    executor="executor-1",
                    run_id=first.id,
                    cwd=str(root),
                    lease_ttl_s=2,
                    heartbeat_interval_s=0.1,
                )
            )
            for _ in range(50):
                state = load_run(repo, first.id)["state"]
                if isinstance(state, dict) and state.get("phase") == "running":
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(load_run(repo, first.id)["state"]["phase"], "running")

            blocked = await execute_once(repo, executor="executor-2", cwd=str(root))
            self.assertIsNone(blocked)
            self.assertEqual([item["id"] for item in list_runs(repo, status="pending")], [second.id])
            snapshot = capacity_snapshot(repo)
            self.assertEqual(snapshot["limits"][0]["max_concurrent"], 1)
            self.assertEqual(len(snapshot["slots"]), 1)

            self.assertIsNotNone(cancel_run(repo, first.id))
            completed = await asyncio.wait_for(first_executor, timeout=3)
            self.assertIsNotNone(completed)
            self.assertEqual(load_run(repo, first.id)["state"]["phase"], "canceled")

            after_release = await execute_once(repo, executor="executor-3", run_id=second.id, cwd=str(root))
            self.assertIsNotNone(after_release)
            assert after_release is not None
            self.assertEqual(after_release["id"], second.id)
            self.assertEqual((root / "second.txt").read_text(encoding="utf-8"), "second")
            self.assertEqual(capacity_snapshot(repo)["slots"], [])

    def test_cancel_and_index_repair_update_derived_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            pending = enqueue_command(repo, "printf nope")
            canceled = cancel_run(repo, pending.id)
            self.assertIsNotNone(canceled)
            self.assertEqual(load_run(repo, pending.id)["state"]["phase"], "canceled")
            self.assertEqual(list_runs(repo, status="pending"), [])
            self.assertEqual([item["id"] for item in list_runs(repo, status="canceled")], [pending.id])

            store(repo).delete_ref(f"{run_index_by_status_prefix(repo, 'canceled')}{pending.id}")
            self.assertEqual(list_runs(repo, status="canceled"), [])
            repaired = repair_indexes(repo)
            self.assertEqual(repaired["repaired"], 1)
            self.assertEqual([item["id"] for item in list_runs(repo, status="canceled")], [pending.id])

    def test_prune_removes_terminal_run_refs_without_touching_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            terminal = enqueue_command(repo, "printf done")
            active = enqueue_command(repo, "printf active")
            cancel_run(repo, terminal.id)
            dry_run = prune_runs(repo, keep_last=0, dry_run=True)
            self.assertEqual(dry_run["runs"], [terminal.id])
            self.assertIsNotNone(store(repo).read_ref(run_state_ref(repo, terminal.id)))
            pruned = prune_runs(repo, keep_last=0)
            self.assertEqual(pruned["deleted_run_refs"], 1)
            self.assertIsNone(store(repo).read_ref(run_state_ref(repo, terminal.id)))
            self.assertIsNotNone(store(repo).read_ref(run_state_ref(repo, active.id)))


if __name__ == "__main__":
    unittest.main()
