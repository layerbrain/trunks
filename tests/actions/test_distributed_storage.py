from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from trunks.actions.backend_store import run_log_ref, run_queue_prefix, run_state_ref, store
from trunks.actions.run import enqueue_command
from trunks.actions.executor import execute_once
from trunks.actions.storage import append_run_logs, claim_run, list_runs, load_run
from trunks.repository import Repository


class DistributedActionsStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_repositories_share_actions_queue_and_results_through_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage_root = root / "storage"
            producer_root = root / "producer"
            executor_root = root / "executor"
            producer_root.mkdir()
            executor_root.mkdir()
            producer = Repository.init(cwd=producer_root, name="demo", backend=str(storage_root))
            executor = Repository.init(cwd=executor_root, name="demo", backend=str(storage_root))

            pending = enqueue_command(producer, "printf shared > result.txt")
            self.assertIsNone(producer.ref(run_state_ref(producer, pending.id)))
            self.assertIsNotNone(store(executor).read_ref(run_state_ref(executor, pending.id)))

            result = await execute_once(executor, executor="executor-a", cwd=str(executor_root))

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["id"], pending.id)
            self.assertEqual((executor_root / "result.txt").read_text(encoding="utf-8"), "shared")
            self.assertEqual(load_run(producer, pending.id)["state"]["phase"], "succeeded")

    async def test_many_executors_claim_shared_queue_once_each(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage_root = root / "storage"
            producer_root = root / "producer"
            producer_root.mkdir()
            producer = Repository.init(cwd=producer_root, name="demo", backend=str(storage_root))
            runs = [enqueue_command(producer, "true") for _ in range(24)]
            executors = []
            for index in range(24):
                executor_root = root / f"executor-{index}"
                executor_root.mkdir()
                repo = Repository.init(cwd=executor_root, name="demo", backend=str(storage_root))
                executors.append(execute_once(repo, executor=f"executor-{index}", cwd=str(executor_root)))

            results = [item for item in await asyncio.gather(*executors) if item is not None]

            self.assertEqual(len(results), len(runs))
            self.assertEqual(len({str(item["id"]) for item in results}), len(runs))
            self.assertEqual(len(list_runs(producer, status="succeeded", limit=100)), len(runs))
            self.assertEqual(list_runs(producer, status="pending"), [])
            self.assertEqual(store(producer).list_refs(run_queue_prefix(producer)), [])

    def test_logs_are_chunk_refs_not_rewritten_into_state_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage_root = root / "storage"
            root.mkdir(exist_ok=True)
            repo = Repository.init(cwd=root, name="demo", backend=str(storage_root))
            pending = enqueue_command(repo, "true")
            claimed = claim_run(
                repo,
                pending.id,
                executor="executor",
                provider="local",
                spec_key=pending.spec.key,
                region="local",
                requested_region=None,
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            token = claimed["state"]["token"]
            assert isinstance(token, str)
            self.assertTrue(
                append_run_logs(
                    repo,
                    pending.id,
                    token=token,
                    logs=[{"stream": "stdout", "seq": 1, "text": "one\n", "ts_ms": 1}],
                )
            )

            state_oid = store(repo).read_ref(run_state_ref(repo, pending.id))
            self.assertIsNotNone(state_oid)
            state = load_run(repo, pending.id)
            self.assertEqual(state["logs"][0]["text"], "one\n")
            self.assertIsNotNone(store(repo).read_ref(run_log_ref(repo, pending.id, 1, 1)))
            raw_state = store(repo).read_blob_payload(state_oid)  # type: ignore[arg-type]
            self.assertNotIn(b"one\\n", raw_state)

    def test_log_replay_orders_attempts_and_deduplicates_within_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage_root = root / "storage"
            repo = Repository.init(cwd=root, name="demo", backend=str(storage_root))
            pending = enqueue_command(repo, "true")
            first = claim_run(
                repo,
                pending.id,
                executor="executor-1",
                provider="local",
                spec_key=pending.spec.key,
                region="local",
                requested_region=None,
                ttl_s=1,
                now_s=100,
            )
            self.assertIsNotNone(first)
            assert first is not None
            first_token = first["state"]["token"]
            assert isinstance(first_token, str)
            self.assertTrue(
                append_run_logs(
                    repo,
                    pending.id,
                    token=first_token,
                    logs=[
                        {"stream": "stdout", "seq": 2, "text": "attempt1-seq2\n", "ts_ms": 2},
                        {"stream": "stdout", "seq": 1, "text": "attempt1-seq1\n", "ts_ms": 1},
                    ],
                )
            )
            second = claim_run(
                repo,
                pending.id,
                executor="executor-2",
                provider="local",
                spec_key=pending.spec.key,
                region="local",
                requested_region=None,
                ttl_s=1,
                now_s=102,
            )
            self.assertIsNotNone(second)
            assert second is not None
            second_token = second["state"]["token"]
            assert isinstance(second_token, str)
            self.assertTrue(
                append_run_logs(
                    repo,
                    pending.id,
                    token=second_token,
                    logs=[{"stream": "stdout", "seq": 1, "text": "attempt2-seq1\n", "ts_ms": 3}],
                )
            )

            self.assertEqual(
                [item["text"] for item in load_run(repo, pending.id)["logs"]],
                ["attempt1-seq1\n", "attempt1-seq2\n", "attempt2-seq1\n"],
            )


if __name__ == "__main__":
    unittest.main()
