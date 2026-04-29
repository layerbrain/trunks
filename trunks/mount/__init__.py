"""NFSv3-localhost virtual mount for Trunks repos.

This package implements a userspace NFSv3 server (RFC 1813), Mount protocol v3
(RFC 1813 Appendix I), ONC RPC v2 framing (RFC 5531), and XDR encoding (RFC
4506). The OS's built-in NFS client mounts the daemon over loopback — no kext,
no macFUSE, no system extension prompt.

The server is a thin shim over `Repository`: NFS reads call `read_file`, NFS
writes call `write_file`, NFS readdir calls `list_dir`, etc. `trunks checkpoint`
reads the same SQLite index and turns mounted writes into a real commit.
"""

from .handles import HandleTable
from .lifecycle import MountError, MountInfo, mount_nfs, unmount_nfs
from .nfs import NfsHandler
from .mountd import MountHandler
from .server import NfsServer

__all__ = [
    "HandleTable",
    "MountError",
    "MountHandler",
    "MountInfo",
    "NfsHandler",
    "NfsServer",
    "mount_nfs",
    "unmount_nfs",
]
