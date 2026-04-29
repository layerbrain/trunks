"""macOS / Linux mount-and-unmount shell-out for the NFSv3 server.

The OS NFS client ultimately runs `mount -t nfs ...` (Linux) or `mount_nfs ...`
(macOS). On Linux the call typically requires root; on macOS the suid binary
lets the user mount loopback NFS without sudo, provided rsize/wsize are sane.

We always pass `nolocks` (don't talk to a portmapper) and pin `port=`/`mountport=`
to the daemon's port so portmap is never consulted.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Test escape hatch: when this env var is "1", we skip the real OS mount/unmount
# shell-out so the daemon supervisor can be exercised on a host without an NFS
# client (or without sudo). Production code never sets this.
_SKIP_OS_MOUNT_ENV = "TRUNKS_NFS_SKIP_OS_MOUNT"
_MOUNT_TIMEOUT_SECONDS = 30.0


class MountError(RuntimeError):
    """Raised when the OS mount/unmount command fails."""


class MountSandboxedError(MountError):
    """The OS mount syscall succeeded but the kernel refuses subsequent ops.

    This happens when the calling shell runs in a sandbox profile that allows
    `mount(2)` but blocks reads/writes/readdirs against an NFS-mounted volume.
    Known examples: Claude Code (`OPERON_SANDBOXED_NETWORK=1`), Docker /
    devcontainers with restrictive seccomp, macOS Seatbelt profiles, MDM
    laptops with NFS-blocking policies.

    The CLI catches this, tears the half-working mount down, and exits with a
    message that points the user at the sandbox rather than at Trunks.
    """


@dataclass(frozen=True)
class MountInfo:
    target: Path
    host: str
    port: int
    export: str


def _mount_command(info: MountInfo) -> list[str]:
    # `rw` is explicit because macOS user-mounted NFS can otherwise leave the
    # mount in a permissions-denied state on first write. `noresvport` lets the
    # client connect from a non-privileged port (no sudo needed). `sec=sys`
    # forces AUTH_SYS so macOS doesn't pick AUTH_NONE from the mountd flavor
    # list and end up routing as the anonymous user.
    options = (
        f"rw,nolocks,vers=3,tcp,port={info.port},mountport={info.port},"
        "noresvport,sec=sys,rsize=1048576,wsize=1048576,actimeo=1,"
        "soft,intr,retrans=2,timeo=20,deadtimeout=5,retrycnt=0"
    )
    spec = f"{info.host}:{info.export}"
    if platform.system() == "Darwin":
        binary = shutil.which("mount_nfs") or "/sbin/mount_nfs"
        return [binary, "-o", options, spec, str(info.target)]
    binary = shutil.which("mount") or "/bin/mount"
    return [binary, "-t", "nfs", "-o", options, spec, str(info.target)]


def _unmount_command(target: Path, *, force: bool = False) -> list[str]:
    if platform.system() == "Darwin":
        binary = shutil.which("umount") or "/sbin/umount"
    else:
        binary = shutil.which("umount") or "/bin/umount"
    if force:
        return [binary, "-f", str(target)]
    return [binary, str(target)]


async def mount_nfs(info: MountInfo) -> None:
    info.target.mkdir(parents=True, exist_ok=True)
    if os.environ.get(_SKIP_OS_MOUNT_ENV) == "1":
        return
    cmd = _mount_command(info)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_MOUNT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise MountError(f"mount timed out after {_MOUNT_TIMEOUT_SECONDS:.0f}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        raise MountError(
            f"mount failed (exit {proc.returncode}): {stderr.decode().strip() or stdout.decode().strip()}\n"
            f"command: {' '.join(cmd)}"
        )


async def unmount_nfs(target: Path, *, force: bool = False) -> None:
    if os.environ.get(_SKIP_OS_MOUNT_ENV) == "1":
        return
    cmd = _unmount_command(target, force=force)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_MOUNT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise MountError(f"unmount timed out after {_MOUNT_TIMEOUT_SECONDS:.0f}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        raise MountError(
            f"unmount failed (exit {proc.returncode}): {stderr.decode().strip() or stdout.decode().strip()}\n"
            f"command: {' '.join(cmd)}"
        )


_PROBE_FILE_NAME = ".trunks-mount-probe"
_PROBE_PAYLOAD = b"trunks-mount-probe-v1"


async def verify_mount_writable(target: Path) -> None:
    """Probe the mount with one write+read+unlink round-trip.

    Catches sandbox profiles that allow `mount(2)` but block subsequent NFS
    operations: every `open()` / `creat()` / `readdir()` returns EPERM before
    the request even reaches our daemon. The mount table shows the entry, but
    nothing useful can happen through it. Without this probe, `trunks mount`
    would return success and the user would see "Operation not permitted" on
    their next `echo > x` and have no idea why.

    A naked `PermissionError` from any step is treated as sandboxed. The
    daemon never produces EPERM at the protocol level — every `proc_create`
    response is `NFS3_OK` or a typed NFS error, neither of which surfaces as
    EPERM through the kernel client. So if the kernel hands us EPERM here it
    is, by elimination, the kernel itself rejecting the op.
    """
    if os.environ.get(_SKIP_OS_MOUNT_ENV) == "1":
        return
    probe = target / _PROBE_FILE_NAME
    try:
        probe.write_bytes(_PROBE_PAYLOAD)
        observed = probe.read_bytes()
        probe.unlink()
    except PermissionError as exc:
        raise MountSandboxedError(_sandbox_message(target, exc)) from exc
    if observed != _PROBE_PAYLOAD:
        raise MountError(
            f"mount probe round-trip mismatch: wrote {len(_PROBE_PAYLOAD)} bytes, "
            f"read {len(observed)} bytes back. The kernel NFS client may be "
            f"serving stale data from a previous mount.\n  target: {target}"
        )


def _sandbox_message(target: Path, exc: BaseException) -> str:
    return (
        "trunks mount: kernel rejected NFS probe write with EPERM.\n"
        "\n"
        "The mount syscall succeeded but the kernel blocks subsequent\n"
        "filesystem operations on the mount. This usually means the calling\n"
        "shell runs in a sandbox that disallows NFS-mounted volumes:\n"
        "  - Claude Code              (env: OPERON_SANDBOXED_NETWORK=1)\n"
        "  - Docker / devcontainers   (restrictive seccomp profile)\n"
        "  - macOS Seatbelt           (sysctl security.mac.sandbox.sentinel set)\n"
        "  - MDM-managed laptops      (NFS-blocking policy)\n"
        "\n"
        "Run the same command from a normal shell (Terminal.app, iTerm,\n"
        "Ghostty, ssh). The mount has been torn down so you can retry.\n"
        f"  target: {target}\n"
        f"  cause:  {exc}"
    )


def is_localhost_nfs_mounted(target: Path) -> bool:
    if os.environ.get(_SKIP_OS_MOUNT_ENV) == "1":
        return target.exists()
    if platform.system() == "Darwin":
        binary = shutil.which("mount") or "/sbin/mount"
    else:
        binary = shutil.which("mount") or "/bin/mount"
    if not binary:
        return False
    proc = subprocess.run([binary], capture_output=True, text=True, check=False)
    resolved = str(target.expanduser().resolve())
    for line in proc.stdout.splitlines():
        if resolved in line and ("127.0.0.1:" in line or "localhost:" in line) and "nfs" in line.lower():
            return True
    return False
