from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from trunks import audit
from trunks.backends.memory import Memory
from trunks.cli import dispatch
from trunks.engine import Engine
from trunks.repository import Repository


class AuditLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_records_audit_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)
                    (root / "a.md").write_text("a\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "first"]), 0)
                repo = Repository.find(root)
                events = audit.read(repo)
                kinds = [event.event for event in events]
                self.assertIn("checkpoint", kinds)
                checkpoint = next(event for event in events if event.event == "checkpoint")
                self.assertEqual(checkpoint.data["message"], "first")
                self.assertEqual(checkpoint.data["branch"], repo.current_branch)
                self.assertTrue(checkpoint.data["commit"])
            finally:
                os.chdir(cwd)

    async def test_branch_tag_rollback_record_audit_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)
                    (root / "a.md").write_text("a\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "first"]), 0)
                    self.assertEqual(await dispatch(["branch", "create", "--name", "feat", "--from", "main"]), 0)
                    self.assertEqual(await dispatch(["branch", "switch", "--name", "feat"]), 0)
                    self.assertEqual(await dispatch(["tag", "create", "--name", "v1", "--at", "main"]), 0)
                    self.assertEqual(await dispatch(["tag", "delete", "--name", "v1"]), 0)
                    self.assertEqual(await dispatch(["branch", "switch", "--name", "main"]), 0)
                    self.assertEqual(await dispatch(["branch", "delete", "--name", "feat"]), 0)
                    self.assertEqual(await dispatch(["rollback", "--to", "main"]), 0)
                repo = Repository.find(root)
                kinds = [event.event for event in audit.read(repo)]
                for expected in ("branch.create", "branch.switch", "tag.create", "tag.delete", "branch.delete", "rollback"):
                    self.assertIn(expected, kinds)
            finally:
                os.chdir(cwd)

    async def test_audit_cli_list_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)
                    (root / "a.md").write_text("a\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "first"]), 0)
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["audit", "list", "--json"]), 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["object"], "list")
                self.assertTrue(any(item["event"] == "checkpoint" for item in payload["data"]))
                for item in payload["data"]:
                    self.assertEqual(item["object"], "audit_event")
                    self.assertIn("id", item)
                    self.assertIn("ts", item)
                    self.assertIn("data", item)
            finally:
                os.chdir(cwd)

    async def test_push_pull_record_audit_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)
                repo = Repository.find(root)
                engine = Engine(repo, Memory())
                await engine.write("README.md", b"# hi\n")
                await engine.commit(message="init")
                await engine.push()
                await engine.pull()
                kinds = [event.event for event in audit.read(repo)]
                self.assertIn("push", kinds)
                self.assertIn("pull", kinds)
            finally:
                os.chdir(cwd)

    async def test_webhook_delivery_records_audit_event(self) -> None:
        received: list[bytes] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                received.append(self.rfile.read(length))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cwd = Path.cwd()
                os.chdir(root)
                try:
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)
                        self.assertEqual(
                            await dispatch(["webhook", "add", f"http://127.0.0.1:{server.server_port}/hook", "--on", "push"]),
                            0,
                        )
                    repo = Repository.find(root)
                    engine = Engine(repo, Memory())
                    await engine.write("README.md", b"# hi\n")
                    await engine.commit(message="init")
                    await engine.push()
                    kinds = [event.event for event in audit.read(repo)]
                    self.assertIn("webhook.delivered", kinds)
                finally:
                    os.chdir(cwd)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
