from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trunks.repository import Repository
from trunks.rpc import dispatch


class ActionsRpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_actions_rpc_enqueue_execute_list_get_cancel_and_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")

            enqueue = await dispatch(
                repo,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "actions.enqueue",
                    "params": {
                        "command": "printf rpc > rpc.txt",
                        "cpu": 1,
                        "memoryGib": 1,
                        "diskGib": 1,
                        "artifacts": ["rpc.txt"],
                    },
                },
            )
            self.assertEqual(enqueue["result"]["state"]["phase"], "pending")
            run_id = enqueue["result"]["id"]

            listed = await dispatch(
                repo,
                {"jsonrpc": "2.0", "id": 2, "method": "actions.list", "params": {"status": "pending"}},
            )
            self.assertEqual(listed["result"][0]["id"], run_id)

            executed = await dispatch(
                repo,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "actions.execute",
                    "params": {"executor": "rpc-executor", "run": run_id, "cwd": str(root)},
                },
            )
            self.assertEqual(executed["result"]["id"], run_id)
            self.assertEqual((root / "rpc.txt").read_text(encoding="utf-8"), "rpc")

            loaded = await dispatch(repo, {"jsonrpc": "2.0", "id": 4, "method": "actions.get", "params": {"run": run_id}})
            self.assertEqual(loaded["result"]["state"]["phase"], "succeeded")

            health = await dispatch(repo, {"jsonrpc": "2.0", "id": 5, "method": "actions.health", "params": {}})
            self.assertEqual(health["result"]["object"], "provider_health_snapshot")

            canceled = await dispatch(repo, {"jsonrpc": "2.0", "id": 6, "method": "actions.cancel", "params": {"run": run_id}})
            self.assertEqual(canceled["result"]["state"]["phase"], "succeeded")


if __name__ == "__main__":
    unittest.main()
