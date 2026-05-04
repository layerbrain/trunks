from __future__ import annotations

import tempfile
from collections.abc import Awaitable
from pathlib import Path

from trunks._auto import run_auto
from .provider import ExecResult, LogChunk, SandboxFile, SandboxProvider, SandboxRequest


def run_provider_contract(provider: SandboxProvider) -> dict[str, object] | Awaitable[dict[str, object]]:
    return run_auto(lambda: run_provider_contract_async(provider))


async def run_provider_contract_async(provider: SandboxProvider) -> dict[str, object]:
    spec = provider.info.specs[0]
    region = sorted(provider.info.regions)[0]
    sandbox = await provider.create(
        SandboxRequest(
            run="contract",
            commit="worktree",
            spec=spec,
            region=region,
            isolation=sorted(provider.info.isolation)[0],
            network="default",
            timeout_s=10,
        )
    )
    try:
        hydrate = [progress async for progress in sandbox.hydrate()]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "contract-input.txt"
            target = root / "contract-output.txt"
            source.write_text("contract-input\n", encoding="utf-8")
            upload = [
                progress
                async for progress in sandbox.upload(
                    (SandboxFile(source=str(source), target="/tmp/trunks-contract-input.txt"),)
                )
            ]
            events = [
                event
                async for event in sandbox.run(
                    [
                        "/bin/sh",
                        "-lc",
                        "cat /tmp/trunks-contract-input.txt && printf 'contract-output\\n' > /tmp/trunks-contract-output.txt",
                    ],
                    {},
                    "/tmp",
                    10,
                )
            ]
            download = [
                progress
                async for progress in sandbox.download(
                    (SandboxFile(source="/tmp/trunks-contract-output.txt", target=str(target)),)
                )
            ]
            result = next((event for event in events if isinstance(event, ExecResult)), None)
            logs = [event for event in events if isinstance(event, LogChunk)]
            passed = result is not None and result.exit_code == 0 and target.read_text(encoding="utf-8") == "contract-output\n"
            return {
                "_schema_version": "trunks.sandboxes.contract.v1",
                "object": "sandbox_provider_contract",
                "provider": provider.info.id,
                "passed": passed,
                "hydrate_events": len(hydrate),
                "upload_events": len(upload),
                "log_events": len(logs),
                "download_events": len(download),
            }
    finally:
        await sandbox.destroy()
