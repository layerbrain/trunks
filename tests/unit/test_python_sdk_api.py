from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trunks import Trunks
from trunks.repository import Repository


class PythonSdkApiTests(unittest.TestCase):
    def test_repos_full_crud_uses_managed_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            with patch.dict(os.environ, {"TRUNKS_HOME": str(home), "TRUNKS_LOCAL_ONLY": "1"}, clear=False):
                client = Trunks(cwd=root)

                created = client.repos.create(name="api-demo")
                self.assertEqual(created["object"], "repo")
                self.assertEqual(created["name"], "api-demo")
                self.assertEqual(Path(created["path"]), (home / "repos" / "api-demo").resolve())

                repos = client.repos.list(limit=20, offset=0)
                self.assertEqual(repos["object"], "list")
                self.assertEqual(repos["limit"], 20)
                self.assertEqual(repos["offset"], 0)
                self.assertEqual(repos["data"][0]["name"], "api-demo")

                fetched = client.repos.get(name="api-demo")
                self.assertEqual(fetched["id"], "api-demo")

                updated = client.repos.update(name="api-demo", backend=str(root / "storage"))
                self.assertEqual(updated["backend"], f"{root / 'storage'}/trunks/api-demo.trunk")

                deleted = client.repos.delete(name="api-demo")
                self.assertEqual(deleted, {"object": "repo", "id": "api-demo", "name": "api-demo", "deleted": True})
                self.assertFalse((home / "repos" / "api-demo" / ".trunks" / "api-demo.trunk").exists())

    def test_repos_branches_and_tags_use_api_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                repo = Repository.init(name="demo")
                repo.set_backend_url("local:///tmp/demo.trunk")
                (root / "README.md").write_text("# hi\n", encoding="utf-8")
                repo.add_all_worktree_files()
                repo.create_commit(message="init")

                client = Trunks(cwd=root)

                repos = client.repos.list()
                self.assertEqual(repos["object"], "list")
                self.assertEqual(repos["data"][0]["object"], "repo")
                self.assertEqual(repos["data"][0]["name"], "demo")
                self.assertEqual(repos["data"][0]["storage"][0]["object"], "storage_target")

                created = client.branches.create(name="feature/auth", from_ref="main")
                self.assertEqual(created["object"], "branch")
                self.assertEqual(created["name"], "feature/auth")
                self.assertEqual(client.branches.get(name="feature/auth")["name"], "feature/auth")
                self.assertEqual(client.branches.update(name="feature/auth", from_ref="main")["name"], "feature/auth")

                branches = client.branches.list(limit=1)
                self.assertEqual(branches["object"], "list")
                self.assertEqual(branches["limit"], 1)
                self.assertEqual(branches["total_count"], 2)
                self.assertTrue(branches["has_more"])
                self.assertEqual(branches["data"][0]["object"], "branch")

                tag = client.tags.create(name="v1", at="main")
                self.assertEqual(tag["object"], "tag")
                self.assertEqual(tag["name"], "v1")
                self.assertEqual(client.tags.get(name="v1")["name"], "v1")
                self.assertEqual(client.tags.update(name="v1", at="main")["name"], "v1")

                tags = client.tags.list()
                self.assertEqual(tags["object"], "list")
                self.assertEqual(tags["data"][0]["object"], "tag")
                self.assertEqual(tags["data"][0]["name"], "v1")

                storage = client.storage.list()
                self.assertEqual(storage["object"], "list")
                self.assertEqual(storage["data"][0]["object"], "storage_target")
                created_storage = client.storage.create(name="backup", url=str(root / "backup"), mirror=True)
                self.assertEqual(created_storage["object"], "storage_target")
                self.assertEqual(client.storage.get(name="backup")["name"], "backup")
                self.assertEqual(client.storage.update(name="backup", url=str(root / "backup2"), mirror=True)["name"], "backup")
                self.assertTrue(client.storage.delete(name="backup")["deleted"])

                webhook = client.webhooks.create(url="https://example.com/trunks", events=["push"])
                self.assertEqual(webhook["object"], "webhook")
                self.assertEqual(client.webhooks.get(id=str(webhook["id"]))["id"], webhook["id"])
                updated_webhook = client.webhooks.update(id=str(webhook["id"]), url="https://example.com/updated")
                self.assertEqual(updated_webhook["url"], "https://example.com/updated")
                self.assertEqual(updated_webhook["events"], ["push"])
                webhooks = client.webhooks.list()
                self.assertEqual(webhooks["object"], "list")
                self.assertEqual(webhooks["data"][0]["object"], "webhook")
                deleted = client.webhooks.delete(id=str(webhook["id"]))
                self.assertEqual(deleted["object"], "webhook")
                self.assertTrue(deleted["deleted"])

                audit_events = client.audit.list()
                self.assertEqual(audit_events["object"], "list")
            finally:
                os.chdir(cwd)


class AsyncPythonSdkApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_client_name_returns_awaitables_inside_running_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                repo = Repository.init(name="demo")
                repo.set_backend_url("local:///tmp/demo.trunk")
                (root / "README.md").write_text("# hi\n", encoding="utf-8")
                repo.add_all_worktree_files()
                repo.create_commit(message="init")

                client = Trunks(cwd=root)
                branch = await client.branches.create(name="agent/run-1", from_ref="main")
                self.assertEqual(branch["object"], "branch")
                self.assertEqual(branch["name"], "agent/run-1")
                self.assertEqual((await client.branches.get(name="agent/run-1"))["name"], "agent/run-1")
                self.assertEqual((await client.branches.update(name="agent/run-1", from_ref="main"))["name"], "agent/run-1")

                branches = await client.branches.list()
                self.assertEqual(branches["object"], "list")
                self.assertTrue(any(item["name"] == "agent/run-1" for item in branches["data"]))

                tag = await client.tags.create(name="v1", at="main")
                self.assertEqual(tag["object"], "tag")
                self.assertEqual((await client.tags.get(name="v1"))["name"], "v1")
                self.assertEqual((await client.tags.update(name="v1", at="main"))["name"], "v1")

                tags = await client.tags.list()
                self.assertEqual(tags["object"], "list")
                self.assertEqual(tags["data"][0]["object"], "tag")

                storage = await client.storage.list()
                self.assertEqual(storage["object"], "list")
                self.assertEqual(storage["data"][0]["object"], "storage_target")
                created_storage = await client.storage.create(name="backup", url=str(root / "backup"), mirror=True)
                self.assertEqual(created_storage["object"], "storage_target")
                self.assertEqual((await client.storage.get(name="backup"))["name"], "backup")
                self.assertEqual((await client.storage.update(name="backup", url=str(root / "backup2"), mirror=True))["name"], "backup")
                self.assertTrue((await client.storage.delete(name="backup"))["deleted"])

                webhook = await client.webhooks.create(url="https://example.com/trunks")
                self.assertEqual(webhook["object"], "webhook")
                self.assertEqual((await client.webhooks.get(id=str(webhook["id"])))["id"], webhook["id"])
                self.assertEqual((await client.webhooks.update(id=str(webhook["id"]), url="https://example.com/updated"))["url"], "https://example.com/updated")
                webhooks = await client.webhooks.list()
                self.assertEqual(webhooks["object"], "list")
                self.assertEqual(webhooks["data"][0]["object"], "webhook")
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
