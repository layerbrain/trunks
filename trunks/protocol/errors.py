"""Canonical JSON-RPC application error codes for the Trunks wire protocol.

Codes -32700 .. -32603 are reserved by the JSON-RPC 2.0 spec; Trunks uses the
-32000 .. -32099 application range, organised per `docs/protocol.md` and the
plan in `~/.claude/plans/compare-tour-lcurrnet-plan-jiggly-allen.md` Section
8.4.

The constants here are the source of truth. Both the Python server (`trunks.rpc`)
and the Node SDK (`trunks-node/src/index.ts`) must agree on the values; any
divergence is caught by the cross-SDK tests in `tests/unit/test_rpc_protocol.py`.
"""

from __future__ import annotations

# JSON-RPC 2.0 reserved codes (mirrored here for callers that need the full set).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Trunks application codes.
PROTOCOL_VERSION_ERROR = -32000
REPO_NOT_FOUND = -32001
REF_NOT_FOUND = -32002
BACKEND_UNAVAILABLE = -32003
CONFLICT = -32004
POLICY_DENIED = -32005
JOURNAL_CORRUPT = -32006
DAEMON_UNREACHABLE = -32007
FILE_NOT_FOUND = -32008


CODE_NAMES: dict[int, str] = {
    PARSE_ERROR: "ParseError",
    INVALID_REQUEST: "InvalidRequest",
    METHOD_NOT_FOUND: "MethodNotFound",
    INVALID_PARAMS: "InvalidParams",
    INTERNAL_ERROR: "InternalError",
    PROTOCOL_VERSION_ERROR: "ProtocolVersionError",
    REPO_NOT_FOUND: "RepoNotFoundError",
    REF_NOT_FOUND: "RefNotFoundError",
    BACKEND_UNAVAILABLE: "BackendUnavailableError",
    CONFLICT: "ConflictError",
    POLICY_DENIED: "PolicyDeniedError",
    JOURNAL_CORRUPT: "JournalCorruptError",
    DAEMON_UNREACHABLE: "DaemonUnreachableError",
    FILE_NOT_FOUND: "FileNotFoundError",
}


__all__ = [
    "CODE_NAMES",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "PROTOCOL_VERSION_ERROR",
    "REPO_NOT_FOUND",
    "REF_NOT_FOUND",
    "BACKEND_UNAVAILABLE",
    "CONFLICT",
    "POLICY_DENIED",
    "JOURNAL_CORRUPT",
    "DAEMON_UNREACHABLE",
    "FILE_NOT_FOUND",
]
