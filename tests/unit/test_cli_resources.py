from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.cli import dispatch
from trunks.repository import Repository


class CliResourceCrudTests(unittest.IsolatedAsyncioTestCase):
    async def test_repo_crud_uses_api_resources_and_managed_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, {"TRUNKS_HOME": str(home), "TRUNKS_LOCAL_ONLY": "1"}, clear=False):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(await dispatch(["repo", "create", "--name", "api-demo", "--json"]), 0)
                    created = json.loads(out.getvalue())
                    self.assertEqual(created["object"], "repo")
                    self.assertEqual(created["name"], "api-demo")
                    self.assertEqual(Path(created["path"]), (home / "repos" / "api-demo").resolve())

                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(await dispatch(["repo", "list", "--json", "--limit", "20", "--offset", "0"]), 0)
                    listed = json.loads(out.getvalue())
                    self.assertEqual(listed["object"], "list")
                    self.assertEqual(listed["limit"], 20)
                    self.assertEqual(listed["offset"], 0)
                    self.assertEqual(listed["data"][0]["name"], "api-demo")

                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(await dispatch(["repo", "get", "--name", "api-demo", "--json"]), 0)
                    self.assertEqual(json.loads(out.getvalue())["id"], "api-demo")

                    storage_root = root / "storage"
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(
                            await dispatch([
                                "repo",
                                "update",
                                "--name",
                                "api-demo",
                                "--backend",
                                str(storage_root),
                                "--json",
                            ]),
                            0,
                        )
                    updated = json.loads(out.getvalue())
                    self.assertEqual(updated["backend"], f"{storage_root}/trunks/api-demo.trunk")

                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(await dispatch(["repo", "delete", "--name", "api-demo", "--json"]), 0)
                    deleted = json.loads(out.getvalue())
                    self.assertEqual(deleted, {"object": "repo", "id": "api-demo", "name": "api-demo", "deleted": True})
                    self.assertFalse((home / "repos" / "api-demo" / ".trunks" / "api-demo.trunk").exists())
            finally:
                os.chdir(cwd)

    async def test_branch_and_tag_get_update_delete_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                    self.assertEqual(await dispatch(["branch", "create", "--name", "feature/auth", "--from", "main"]), 0)
                    self.assertEqual(await dispatch(["tag", "create", "--name", "v1", "--at", "main"]), 0)

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["branch", "get", "--name", "feature/auth", "--json"]), 0)
                branch = json.loads(out.getvalue())
                self.assertEqual(branch["object"], "branch")
                self.assertEqual(branch["name"], "feature/auth")

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["branch", "update", "--name", "feature/auth", "--from", "main", "--json"]), 0)
                self.assertEqual(json.loads(out.getvalue())["name"], "feature/auth")

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["tag", "get", "--name", "v1", "--json"]), 0)
                tag = json.loads(out.getvalue())
                self.assertEqual(tag["object"], "tag")
                self.assertEqual(tag["name"], "v1")

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["tag", "update", "--name", "v1", "--at", "main", "--json"]), 0)
                self.assertEqual(json.loads(out.getvalue())["name"], "v1")
            finally:
                os.chdir(cwd)

    async def test_storage_crud_json_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(
                        await dispatch(["storage", "create", "--name", "primary", "--url", str(root / "remote"), "--json"]),
                        0,
                    )
                created = json.loads(out.getvalue())
                self.assertEqual(created["object"], "storage_target")
                self.assertEqual(created["name"], "primary")

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["storage", "get", "--name", "primary", "--json"]), 0)
                self.assertEqual(json.loads(out.getvalue())["name"], "primary")

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(
                        await dispatch(["storage", "update", "--name", "primary", "--url", str(root / "remote2"), "--json"]),
                        0,
                    )
                updated = json.loads(out.getvalue())
                self.assertEqual(updated["url"], f"{root / 'remote2'}/trunks/demo.trunk")

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["storage", "list", "--json", "--limit", "20", "--offset", "0"]), 0)
                listed = json.loads(out.getvalue())
                self.assertEqual(listed["object"], "list")
                self.assertEqual(listed["data"][0]["object"], "storage_target")

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["storage", "delete", "--name", "primary", "--json"]), 0)
                self.assertEqual(json.loads(out.getvalue())["deleted"], True)
            finally:
                os.chdir(cwd)

    async def test_webhook_crud_json_resources_and_update_preserves_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(
                        await dispatch([
                            "webhook",
                            "create",
                            "https://example.com/one",
                            "--on",
                            "push,checkpoint",
                            "--json",
                        ]),
                        0,
                    )
                created = json.loads(out.getvalue())
                hook_id = created["id"]
                self.assertEqual(created["events"], ["push", "checkpoint"])

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["webhook", "update", hook_id, "--url", "https://example.com/two", "--json"]), 0)
                updated = json.loads(out.getvalue())
                self.assertEqual(updated["url"], "https://example.com/two")
                self.assertEqual(updated["events"], ["push", "checkpoint"])

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["webhook", "get", hook_id, "--json"]), 0)
                self.assertEqual(json.loads(out.getvalue())["id"], hook_id)

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["webhook", "delete", hook_id, "--json"]), 0)
                self.assertEqual(json.loads(out.getvalue())["deleted"], True)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
