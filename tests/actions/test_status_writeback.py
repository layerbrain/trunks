from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from trunks.actions.status_writeback import github_checks_writeback, github_checks_writeback_from_env


class StatusWritebackTests(unittest.TestCase):
    def test_github_checks_writeback_posts_check_run_payload(self) -> None:
        seen: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                seen["path"] = self.path
                seen["authorization"] = self.headers["Authorization"]
                seen["body"] = json.loads(self.rfile.read(length).decode())
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id": 123}')

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        try:
            result = github_checks_writeback(
                token="token",
                owner="acme",
                repo="app",
                commit="abc123",
                run="run-1",
                name="Trunks Actions",
                conclusion="succeeded",
                api_base=f"http://127.0.0.1:{server.server_port}",
            )
        finally:
            thread.join(timeout=5)
            server.server_close()
        self.assertEqual(result["id"], 123)
        self.assertEqual(seen["path"], "/repos/acme/app/check-runs")
        self.assertEqual(seen["authorization"], "Bearer token")
        self.assertEqual(seen["body"]["external_id"], "run-1")
        self.assertEqual(seen["body"]["conclusion"], "success")

    def test_github_checks_writeback_from_env_is_explicitly_configured(self) -> None:
        seen: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                seen["path"] = self.path
                seen["body"] = json.loads(self.rfile.read(length).decode())
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id": 456}')

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        run = {
            "id": "run-2",
            "commit": "abc456",
            "state": {"phase": "failed"},
        }
        try:
            with patch.dict(
                "os.environ",
                {
                    "TRUNKS_GITHUB_CHECKS_TOKEN": "token",
                    "TRUNKS_GITHUB_REPOSITORY": "acme/app",
                    "TRUNKS_GITHUB_API_BASE": f"http://127.0.0.1:{server.server_port}",
                    "TRUNKS_ACTIONS_DETAILS_URL": "https://trunks.local/runs/{run}",
                },
            ):
                result = github_checks_writeback_from_env(run)
        finally:
            thread.join(timeout=5)
            server.server_close()

        self.assertEqual(result["id"], 456)
        self.assertEqual(seen["path"], "/repos/acme/app/check-runs")
        self.assertEqual(seen["body"]["details_url"], "https://trunks.local/runs/run-2")
        self.assertEqual(seen["body"]["conclusion"], "failure")


if __name__ == "__main__":
    unittest.main()
