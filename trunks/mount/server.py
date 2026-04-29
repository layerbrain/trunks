"""asyncio NFSv3 + Mount protocol server.

Binds to 127.0.0.1 on a single TCP port and dispatches by RPC program number.
The macOS/Linux NFS clients accept `port=N,mountport=N` so we can serve both
protocols from one listener.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

from ..repository import Repository
from . import mountd, nfs
from .handles import HandleTable
from .rpc import (
    RpcError,
    encode_record,
    pack_reply_accepted_error,
    pack_reply_prog_mismatch,
    pack_reply_rpc_mismatch,
    parse_call,
    read_record,
    PROG_UNAVAIL,
)

logger = logging.getLogger(__name__)

# Loopback NFS sends ~32 KB WRITE RPCs back-to-back. Without TCP_NODELAY the
# kernel's Nagle algorithm holds each tiny reply up to 40 ms waiting for more
# bytes to coalesce — for a 50 MB write that's 1600 RPCs × 40 ms = 64 seconds
# of pure stall. Disabling Nagle is the single biggest perf knob on the data
# path. The 1 MB socket buffers keep streaming writes from blocking in the
# loopback socket pair when the daemon falls one chunk behind.
_SOCKET_BUFFER_BYTES = 4 << 20


class NfsServer:
    """Hosts the NFSv3 + Mount handlers over a TCP server bound to localhost."""

    def __init__(
        self,
        repo: Repository,
        export_name: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.repo = repo
        self.export_name = export_name
        self.host = host
        self.port = port
        self.handles = HandleTable()
        self.nfs = nfs.NfsHandler(repo, self.handles)
        self.mountd = mountd.MountHandler(export_name, self.handles)
        self._server: Optional[asyncio.base_events.Server] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, host=self.host, port=self.port)
        sock = self._server.sockets[0] if self._server.sockets else None
        if sock is not None:
            self.port = sock.getsockname()[1]

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self.nfs.close()
        self._server = None

    def disable_mount_protocol(self) -> None:
        self.mountd.disable_mnt()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SOCKET_BUFFER_BYTES)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCKET_BUFFER_BYTES)
        try:
            while True:
                try:
                    record = await read_record(reader)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    return
                except RpcError as err:
                    logger.warning("trunks-nfs: malformed record from %s: %s", peer, err)
                    return
                reply = self._dispatch(record)
                writer.write(encode_record(reply))
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _dispatch(self, record: bytes) -> bytes:
        try:
            call = parse_call(record)
        except RpcError:
            # Without a parsable xid we can only drop. Reply with a synthetic
            # rpc-mismatch keyed on xid 0 so misbehaving clients get a hint.
            return pack_reply_rpc_mismatch(0, 2, 2)
        if call.rpcvers != 2:
            return pack_reply_rpc_mismatch(call.xid, 2, 2)
        if call.prog == nfs.PROGRAM:
            if call.vers != nfs.VERSION:
                return pack_reply_prog_mismatch(call.xid, nfs.VERSION, nfs.VERSION)
            return self.nfs.dispatch(call)
        if call.prog == mountd.PROGRAM:
            if call.vers != mountd.VERSION:
                return pack_reply_prog_mismatch(call.xid, mountd.VERSION, mountd.VERSION)
            return self.mountd.dispatch(call)
        return pack_reply_accepted_error(call.xid, PROG_UNAVAIL)
