from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from trunks import protocol, runtime
from trunks.client import RpcClientError, UnixSocketClient
from trunks.protocol import errors as proto_errors
from trunks.repository import Repository


class RpcSocketLifecycleTests(unittest.TestCase):
    """Unix domain socket lifecycle, mode 0600, stale recovery, concurrent clients."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repository.init(Path(self._tmp.name) / "demo", name="demo")

    def _start(self) -> runtime.RuntimeStatus:
        status = runtime.start(self.repo, interval_seconds=10.0)
        self.addCleanup(self._stop)
        return status

    def _stop(self) -> None:
        runtime.stop(self.repo)

    def test_socket_is_created_with_owner_only_mode(self) -> None:
        status = self._start()
        self.assertTrue(status.running)
        sock_path = runtime.socket_path(self.repo)
        self.assertTrue(sock_path.exists())
        # POSIX owner-only socket: 0600 (no group/other access).
        mode = stat.S_IMODE(sock_path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_handshake_round_trip_through_unix_socket(self) -> None:
        self._start()
        client = UnixSocketClient(runtime.socket_path(self.repo))
        result = client.call("hello.handshake", {"protocolVersion": protocol.PROTOCOL_VERSION})
        self.assertEqual(result["protocolVersion"], protocol.PROTOCOL_VERSION)
        self.assertEqual(result["repository"], "demo")

    def test_protocol_mismatch_raises_typed_error(self) -> None:
        self._start()
        client = UnixSocketClient(runtime.socket_path(self.repo))
        with self.assertRaises(RpcClientError) as ctx:
            client.call("hello.handshake", {"protocolVersion": "00-bogus"})
        self.assertEqual(ctx.exception.code, proto_errors.PROTOCOL_VERSION_ERROR)
        assert ctx.exception.data is not None
        self.assertEqual(ctx.exception.data["server"], protocol.PROTOCOL_VERSION)

    def test_stale_pid_file_is_recovered_on_restart(self) -> None:
        # Simulate a daemon that died without unlinking its pid/sock files.
        pid_path = runtime.runtime_dir(self.repo) / "daemon.pid"
        sock_path = runtime.socket_path(self.repo)
        runtime.runtime_dir(self.repo).mkdir(parents=True, exist_ok=True)
        pid_path.write_text("999999\n", encoding="utf-8")  # PID guaranteed not alive
        sock_path.touch()
        status = self._start()
        self.assertTrue(status.running)
        # New daemon owns a fresh PID; the stale 999999 was discarded.
        self.assertNotEqual(status.pid, 999999)

    def test_starting_when_already_running_returns_existing(self) -> None:
        first = self._start()
        second = runtime.start(self.repo, interval_seconds=10.0)
        self.assertEqual(first.pid, second.pid)

    def test_concurrent_clients_share_one_daemon(self) -> None:
        self._start()
        sock_path = runtime.socket_path(self.repo)
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def call() -> None:
            try:
                client = UnixSocketClient(sock_path)
                result = client.call("hello.handshake", {"protocolVersion": protocol.PROTOCOL_VERSION})
                results.append(result)
            except BaseException as exc:  # surface in main thread
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        for result in results:
            self.assertEqual(result["repository"], "demo")

    def test_unknown_method_returns_method_not_found_over_socket(self) -> None:
        self._start()
        client = UnixSocketClient(runtime.socket_path(self.repo))
        with self.assertRaises(RpcClientError) as ctx:
            client.call("not.a.method", {})
        self.assertEqual(ctx.exception.code, proto_errors.METHOD_NOT_FOUND)

    def test_fs_write_over_socket_updates_real_worktree(self) -> None:
        self._start()
        client = UnixSocketClient(runtime.socket_path(self.repo))
        result = client.call("fs.write", {"path": "task.md", "text": "Fix auth\n"})
        self.assertEqual(result, {"ok": True})
        self.assertEqual((self.repo.root / "task.md").read_text(encoding="utf-8"), "Fix auth\n")


class RpcSocketRawTests(unittest.TestCase):
    """Raw-socket framing checks that bypass the Python client."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repository.init(Path(self._tmp.name) / "demo", name="demo")
        runtime.start(self.repo, interval_seconds=10.0)
        self.addCleanup(lambda: runtime.stop(self.repo))

    def _send(self, payload: bytes) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(runtime.socket_path(self.repo)))
            client.sendall(payload)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        return b"".join(chunks)

    def test_garbage_payload_returns_parse_error(self) -> None:
        raw = self._send(b"this-is-not-json\n")
        response = json.loads(raw)
        self.assertEqual(response["error"]["code"], proto_errors.PARSE_ERROR)
        self.assertEqual(response["jsonrpc"], "2.0")

    def test_well_formed_handshake_round_trip(self) -> None:
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "hello.handshake",
                "params": {"protocolVersion": protocol.PROTOCOL_VERSION},
            }
        ).encode("utf-8") + b"\n"
        response = json.loads(self._send(request))
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["result"]["repository"], "demo")

    def test_shutdown_only_uses_request_method_not_payload_substring(self) -> None:
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "hello.handshake",
                "params": {
                    "protocolVersion": protocol.PROTOCOL_VERSION,
                    "note": "daemon.shutdown",
                },
            }
        ).encode("utf-8") + b"\n"
        response = json.loads(self._send(request))
        self.assertEqual(response["result"]["repository"], "demo")

        follow_up = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "hello.handshake",
                "params": {"protocolVersion": protocol.PROTOCOL_VERSION},
            }
        ).encode("utf-8") + b"\n"
        response = json.loads(self._send(follow_up))
        self.assertEqual(response["result"]["repository"], "demo")


if __name__ == "__main__":
    unittest.main()
