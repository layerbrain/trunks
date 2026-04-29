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

from trunks import runtime
from trunks.backends.memory import Memory
from trunks.cli import dispatch
from trunks.engine import Engine
from trunks.repository import Repository


class RuntimeWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_scan_journals_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(root, name="demo")
            (root / "README.md").write_text("one\n", encoding="utf-8")

            self.assertEqual(runtime.scan_once(repo), 1)
            self.assertEqual(runtime.scan_once(repo), 0)
            (root / "README.md").write_text("two\n", encoding="utf-8")
            self.assertEqual(runtime.scan_once(repo), 1)

    async def test_mount_watch_starts_and_unmount_stops_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "demo"
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    await dispatch(["mount", "--repo", "demo", "--path", str(target), "--watch"]),
                    0,
                )
            repo = Repository.find(target)
            self.assertTrue(runtime.status(repo).running)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(await dispatch(["unmount", "--path", str(target)]), 0)
            self.assertFalse(runtime.status(repo).running)

    async def test_webhook_cli_and_push_delivery(self) -> None:
        received: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                received.append(json.loads(self.rfile.read(length)))
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
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(await dispatch(["webhook", "list", "--json"]), 0)
                    payload = json.loads(out.getvalue())
                    self.assertEqual(payload["object"], "list")
                    self.assertEqual(payload["data"][0]["object"], "webhook")
                    repo = Repository.find(root)
                    engine = Engine(repo, Memory())
                    await engine.write("README.md", b"# hi\n")
                    await engine.commit(message="init")
                    await engine.push()
                finally:
                    os.chdir(cwd)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["event"], "push")
            self.assertEqual(received[0]["repository"], "demo")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
