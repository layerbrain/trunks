from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from . import _speed


class RpcClientError(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class UnixSocketClient:
    def __init__(self, socket_path: str | Path) -> None:
        self.socket_path = Path(socket_path)
        self._next_id = 1

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.socket_path))
            client.sendall(_speed.dumps(payload) + b"\n")
            raw = _readline(client)
        response = _speed.loads(raw)
        if "error" in response:
            error = response["error"]
            raise RpcClientError(int(error["code"]), str(error["message"]), error.get("data"))
        return response.get("result")


def _readline(client: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = client.recv(1)
        if not chunk:
            break
        if chunk == b"\n":
            break
        chunks.append(chunk)
    return b"".join(chunks)
