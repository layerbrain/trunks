from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from trunks import protocol
from trunks.errors import RefConflict, RepositoryNotFound
from trunks.protocol import errors as proto_errors
from trunks.repository import Repository
from trunks.rpc import RpcError, _rpc_error, dispatch, dispatch_line


_NODE_INDEX = Path(__file__).resolve().parents[2] / "trunks-node" / "src" / "index.ts"


class RpcProtocolFramingTests(unittest.IsolatedAsyncioTestCase):
    """JSON-RPC 2.0 framing, dispatch, and error-code mapping."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repository.init(Path(self._tmp.name), name="demo")

    async def test_parse_error_when_payload_is_invalid_json(self) -> None:
        response = json.loads(await dispatch_line(self.repo, b"not-json"))
        self.assertEqual(response["error"]["code"], proto_errors.PARSE_ERROR)
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertIsNone(response["id"])

    async def test_invalid_request_when_payload_is_not_an_object(self) -> None:
        response = json.loads(await dispatch_line(self.repo, b'["not","an","object"]'))
        self.assertEqual(response["error"]["code"], proto_errors.INVALID_REQUEST)
        self.assertEqual(response["jsonrpc"], "2.0")

    async def test_invalid_request_when_jsonrpc_version_missing(self) -> None:
        response = await dispatch(self.repo, {"id": 1, "method": "hello.handshake"})
        assert response is not None
        self.assertEqual(response["error"]["code"], proto_errors.INVALID_REQUEST)

    async def test_method_not_found_returns_minus_32601(self) -> None:
        response = await dispatch(
            self.repo,
            {"jsonrpc": "2.0", "id": 1, "method": "does.not.exist", "params": {}},
        )
        assert response is not None
        self.assertEqual(response["error"]["code"], proto_errors.METHOD_NOT_FOUND)

    async def test_invalid_params_when_params_is_not_object(self) -> None:
        response = await dispatch(
            self.repo,
            {"jsonrpc": "2.0", "id": 1, "method": "hello.handshake", "params": []},
        )
        assert response is not None
        self.assertEqual(response["error"]["code"], proto_errors.INVALID_PARAMS)

    async def test_handshake_returns_protocol_version_and_repo(self) -> None:
        response = await dispatch(
            self.repo,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "hello.handshake",
                "params": {"protocolVersion": protocol.PROTOCOL_VERSION},
            },
        )
        assert response is not None
        self.assertEqual(response["result"]["protocolVersion"], protocol.PROTOCOL_VERSION)
        self.assertEqual(response["result"]["repository"], "demo")

    async def test_handshake_mismatch_returns_protocol_version_error(self) -> None:
        response = await dispatch(
            self.repo,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "hello.handshake",
                "params": {"protocolVersion": "00-bogus"},
            },
        )
        assert response is not None
        self.assertEqual(response["error"]["code"], proto_errors.PROTOCOL_VERSION_ERROR)
        self.assertEqual(response["error"]["data"]["server"], protocol.PROTOCOL_VERSION)
        self.assertEqual(response["error"]["data"]["client"], "00-bogus")

    async def test_notification_without_id_returns_no_response(self) -> None:
        # JSON-RPC 2.0 notifications: no id → no response, even on error.
        line = await dispatch_line(self.repo, b'{"jsonrpc":"2.0","method":"hello.handshake","params":{}}')
        self.assertEqual(line, b"")
        line_err = await dispatch_line(self.repo, b'{"jsonrpc":"2.0","method":"unknown.method"}')
        self.assertEqual(line_err, b"")

    async def test_fs_write_updates_real_worktree_and_journal(self) -> None:
        response = await dispatch(
            self.repo,
            {"jsonrpc": "2.0", "id": 1, "method": "fs.write", "params": {"path": "task.md", "text": "Fix auth\n"}},
        )
        assert response is not None
        self.assertEqual(response["result"], {"ok": True})
        self.assertEqual((self.repo.root / "task.md").read_text(encoding="utf-8"), "Fix auth\n")
        self.assertEqual(self.repo.read_file("task.md"), b"Fix auth\n")
        with self.repo._connect() as conn:
            rows = conn.execute("select data from journals where kind = 'worktree'").fetchall()
        self.assertEqual(len(rows), 1)
        journal = json.loads(rows[0]["data"])
        self.assertEqual(journal["data"]["op"], "write")
        self.assertEqual(journal["data"]["path"], "task.md")

    async def test_fs_remove_updates_real_worktree_index_and_read_error(self) -> None:
        await dispatch(
            self.repo,
            {"jsonrpc": "2.0", "id": 1, "method": "fs.write", "params": {"path": "task.md", "text": "Fix auth\n"}},
        )
        response = await dispatch(
            self.repo,
            {"jsonrpc": "2.0", "id": 2, "method": "fs.remove", "params": {"path": "task.md"}},
        )
        assert response is not None
        self.assertEqual(response["result"], {"ok": True})
        self.assertFalse((self.repo.root / "task.md").exists())
        self.assertFalse(any(entry.path == "task.md" for entry in self.repo.index_entries()))
        read_response = await dispatch(
            self.repo,
            {"jsonrpc": "2.0", "id": 3, "method": "fs.read", "params": {"path": "task.md"}},
        )
        assert read_response is not None
        self.assertEqual(read_response["error"]["code"], proto_errors.FILE_NOT_FOUND)


class RpcErrorMappingTests(unittest.TestCase):
    """Domain exception → RPC error code mapping."""

    def test_repository_not_found_maps_to_repo_not_found(self) -> None:
        rpc = _rpc_error(RepositoryNotFound("demo"))
        self.assertEqual(rpc.code, proto_errors.REPO_NOT_FOUND)

    def test_ref_conflict_maps_to_conflict(self) -> None:
        rpc = _rpc_error(RefConflict("expected oid != actual oid"))
        self.assertEqual(rpc.code, proto_errors.CONFLICT)

    def test_passthrough_rpc_error(self) -> None:
        original = RpcError(proto_errors.POLICY_DENIED, "denied")
        self.assertIs(_rpc_error(original), original)


class CodeParityTests(unittest.TestCase):
    """Python ↔ Node SDK must agree on every application error code."""

    def test_node_error_code_table_matches_python(self) -> None:
        node_text = _NODE_INDEX.read_text(encoding="utf-8")
        match = re.search(r"export const ErrorCode = \{(.*?)\} as const;", node_text, re.DOTALL)
        self.assertIsNotNone(match, "ErrorCode table missing in trunks-node/src/index.ts")
        body = match.group(1) if match else ""
        node_codes = {
            name: int(value)
            for name, value in re.findall(r"(\w+):\s*(-?\d+),", body)
        }
        expected = {
            "ParseError": proto_errors.PARSE_ERROR,
            "InvalidRequest": proto_errors.INVALID_REQUEST,
            "MethodNotFound": proto_errors.METHOD_NOT_FOUND,
            "InvalidParams": proto_errors.INVALID_PARAMS,
            "InternalError": proto_errors.INTERNAL_ERROR,
            "ProtocolVersion": proto_errors.PROTOCOL_VERSION_ERROR,
            "RepoNotFound": proto_errors.REPO_NOT_FOUND,
            "RefNotFound": proto_errors.REF_NOT_FOUND,
            "BackendUnavailable": proto_errors.BACKEND_UNAVAILABLE,
            "Conflict": proto_errors.CONFLICT,
            "PolicyDenied": proto_errors.POLICY_DENIED,
            "JournalCorrupt": proto_errors.JOURNAL_CORRUPT,
            "DaemonUnreachable": proto_errors.DAEMON_UNREACHABLE,
            "FileNotFound": proto_errors.FILE_NOT_FOUND,
        }
        self.assertEqual(node_codes, expected)

    def test_application_codes_are_distinct_and_in_jsonrpc_app_range(self) -> None:
        codes = [
            proto_errors.PROTOCOL_VERSION_ERROR,
            proto_errors.REPO_NOT_FOUND,
            proto_errors.REF_NOT_FOUND,
            proto_errors.BACKEND_UNAVAILABLE,
            proto_errors.CONFLICT,
            proto_errors.POLICY_DENIED,
            proto_errors.JOURNAL_CORRUPT,
            proto_errors.DAEMON_UNREACHABLE,
            proto_errors.FILE_NOT_FOUND,
        ]
        self.assertEqual(len(codes), len(set(codes)))
        for code in codes:
            self.assertGreaterEqual(code, -32099)
            self.assertLessEqual(code, -32000)


if __name__ == "__main__":
    unittest.main()
