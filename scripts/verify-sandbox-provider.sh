#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || "${1:-}" != "--name" ]]; then
  echo "usage: scripts/verify-sandbox-provider.sh --name <provider> [--live]" >&2
  exit 2
fi

provider="$2"
mode="${3:-}"
python_bin="${PYTHON:-python3.12}"

if [[ "$provider" == "daytona" && "$mode" != "--live" && -z "${DAYTONA_API_KEY:-}" ]]; then
  export DAYTONA_API_KEY="metadata-only"
fi

"$python_bin" -m compileall -q trunks tests/sandboxes
"$python_bin" - <<PY
import asyncio
from trunks.sandboxes.cli import dispatch

async def main():
    for argv in (
        ["providers", "show", "--name", "$provider", "--json"],
        ["providers", "doctor", "--name", "$provider", "--json"],
    ):
        code = await dispatch(argv)
        if code:
            raise SystemExit(code)

asyncio.run(main())
PY

if [[ "$mode" == "--live" ]]; then
  "$python_bin" - <<PY
import asyncio
from trunks.sandboxes.cli import dispatch

async def main():
    await dispatch(["providers", "cleanup", "--name", "$provider", "--prefix", "trunks-", "--json"])
    try:
        code = await dispatch(["providers", "test", "--name", "$provider", "--live", "--json"])
        if code:
            raise SystemExit(code)
    finally:
        await dispatch(["providers", "cleanup", "--name", "$provider", "--prefix", "trunks-", "--json"])

asyncio.run(main())
PY
elif [[ -n "$mode" ]]; then
  echo "unknown mode: $mode" >&2
  exit 2
else
  echo "metadata checks passed for '$provider'. Add --live to create a real sandbox and run the provider contract."
fi
