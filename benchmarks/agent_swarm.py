from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trunks.backends.local import Local
from trunks.errors import RefConflict
from trunks.push import Push
from trunks.repository import Repository


Mode = Literal["trunks", "git", "all"]
Scenario = Literal["branches", "same-branch"]


@dataclass(frozen=True)
class ChangeResult:
    ok: bool
    seconds: float
    detail: str = ""


@dataclass(frozen=True)
class BenchmarkResult:
    name: str
    scenario: str
    changes: int
    workers: int
    successes: int
    failures: int
    wall_seconds: float
    changes_per_second: float
    p50_ms: float
    p95_ms: float
    store_bytes: int
    detail: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark agent-style concurrent repository changes.")
    parser.add_argument("--mode", choices=["trunks", "git", "all"], default="all")
    parser.add_argument("--scenario", choices=["branches", "same-branch"], default="branches")
    parser.add_argument("--changes", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path(args.workdir).expanduser().resolve() if args.workdir else Path(tempfile.mkdtemp(prefix="trunks-bench-"))
    root.mkdir(parents=True, exist_ok=True)

    results: list[BenchmarkResult] = []
    if args.mode in {"trunks", "all"}:
        results.append(
            run_trunks_benchmark(
                root / "trunks",
                scenario=args.scenario,
                changes=args.changes,
                workers=args.workers,
                payload_bytes=args.payload_bytes,
            )
        )
    if args.mode in {"git", "all"}:
        if shutil.which("git") is None:
            results.append(
                BenchmarkResult(
                    name="git",
                    scenario=args.scenario,
                    changes=args.changes,
                    workers=args.workers,
                    successes=0,
                    failures=args.changes,
                    wall_seconds=0.0,
                    changes_per_second=0.0,
                    p50_ms=0.0,
                    p95_ms=0.0,
                    store_bytes=0,
                    detail="git executable not found",
                )
            )
        else:
            results.append(
                run_git_benchmark(
                    root / "git",
                    scenario=args.scenario,
                    changes=args.changes,
                    workers=args.workers,
                    payload_bytes=args.payload_bytes,
                )
            )

    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2, sort_keys=True))
    else:
        print_results(results, root)
    return 0


def run_trunks_benchmark(
    root: Path,
    *,
    scenario: Scenario,
    changes: int,
    workers: int,
    payload_bytes: int,
) -> BenchmarkResult:
    shutil.rmtree(root, ignore_errors=True)
    store = root / "store" / "bench.trunk"
    clients = root / "clients"
    clients.mkdir(parents=True, exist_ok=True)
    store.mkdir(parents=True, exist_ok=True)

    start = time.perf_counter()
    results = _run_pool(
        changes=changes,
        workers=workers,
        fn=lambda index: _trunks_change(index, clients, store, scenario=scenario, payload_bytes=payload_bytes),
    )
    wall = time.perf_counter() - start
    return summarize(
        name="trunks",
        scenario=scenario,
        changes=changes,
        workers=workers,
        wall_seconds=wall,
        results=results,
        store=root,
        detail=_trunks_detail(store, scenario),
    )


def _trunks_change(index: int, clients: Path, store: Path, *, scenario: Scenario, payload_bytes: int) -> ChangeResult:
    start = time.perf_counter()
    root = clients / f"agent-{index:06d}"
    root.mkdir(parents=True, exist_ok=True)
    branch = f"agent/{index:06d}" if scenario == "branches" else "main"
    payload = _payload(index, payload_bytes)
    try:
        repo = Repository.init(root, name="bench", backend=f"local://{store}")
        repo.set_current_branch(branch)
        repo.write_file(f"agents/{index:06d}.txt", payload)
        repo.create_commit(message=f"agent {index:06d}")
        asyncio.run(Push(repo, Local(store)).run())
        return ChangeResult(True, time.perf_counter() - start)
    except RefConflict as exc:
        return ChangeResult(False, time.perf_counter() - start, str(exc))
    except Exception as exc:
        return ChangeResult(False, time.perf_counter() - start, f"{type(exc).__name__}: {exc}")


def run_git_benchmark(
    root: Path,
    *,
    scenario: Scenario,
    changes: int,
    workers: int,
    payload_bytes: int,
) -> BenchmarkResult:
    shutil.rmtree(root, ignore_errors=True)
    remote = root / "remote.git"
    clients = root / "clients"
    clients.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "--bare", str(remote)], cwd=root, check=True)

    start = time.perf_counter()
    results = _run_pool(
        changes=changes,
        workers=workers,
        fn=lambda index: _git_change(index, clients, remote, scenario=scenario, payload_bytes=payload_bytes),
    )
    wall = time.perf_counter() - start
    return summarize(
        name="git",
        scenario=scenario,
        changes=changes,
        workers=workers,
        wall_seconds=wall,
        results=results,
        store=remote,
        detail="local bare git remote; GitHub adds network, auth, API, PR, and multi-tenant control-plane work",
    )


def _git_change(index: int, clients: Path, remote: Path, *, scenario: Scenario, payload_bytes: int) -> ChangeResult:
    start = time.perf_counter()
    root = clients / f"agent-{index:06d}"
    branch = f"agent/{index:06d}" if scenario == "branches" else "main"
    payload = _payload(index, payload_bytes).decode("utf-8")
    try:
        _run(["git", "clone", "--quiet", str(remote), str(root)], cwd=clients, check=True)
        _run(["git", "config", "user.email", "bench@example.com"], cwd=root, check=True)
        _run(["git", "config", "user.name", "Trunks Bench"], cwd=root, check=True)
        _run(["git", "checkout", "-B", branch], cwd=root, check=True)
        path = root / "agents" / f"{index:06d}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        _run(["git", "add", "agents"], cwd=root, check=True)
        _run(["git", "commit", "--quiet", "-m", f"agent {index:06d}"], cwd=root, check=True)
        push = _run(["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=root, check=False)
        if push.returncode != 0:
            return ChangeResult(False, time.perf_counter() - start, push.stderr.strip() or push.stdout.strip())
        return ChangeResult(True, time.perf_counter() - start)
    except Exception as exc:
        return ChangeResult(False, time.perf_counter() - start, f"{type(exc).__name__}: {exc}")


def _run_pool(*, changes: int, workers: int, fn: Callable[[int], ChangeResult]) -> list[ChangeResult]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fn, index) for index in range(changes)]
        return [future.result() for future in as_completed(futures)]


def summarize(
    *,
    name: str,
    scenario: str,
    changes: int,
    workers: int,
    wall_seconds: float,
    results: list[ChangeResult],
    store: Path,
    detail: str,
) -> BenchmarkResult:
    successes = sum(1 for result in results if result.ok)
    failures = len(results) - successes
    durations = sorted(result.seconds for result in results)
    p50 = statistics.median(durations) * 1000 if durations else 0.0
    p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))] * 1000 if durations else 0.0
    return BenchmarkResult(
        name=name,
        scenario=scenario,
        changes=changes,
        workers=workers,
        successes=successes,
        failures=failures,
        wall_seconds=wall_seconds,
        changes_per_second=successes / wall_seconds if wall_seconds > 0 else 0.0,
        p50_ms=p50,
        p95_ms=p95,
        store_bytes=_dir_size(store),
        detail=detail if failures == 0 else f"{detail}; sample failure: {next((r.detail for r in results if not r.ok), '')}",
    )


def print_results(results: list[BenchmarkResult], root: Path) -> None:
    print(f"workspace: {root}")
    print()
    for result in results:
        print(f"{result.name} / {result.scenario}")
        print(f"  changes           {result.changes}")
        print(f"  workers           {result.workers}")
        print(f"  successes         {result.successes}")
        print(f"  failures          {result.failures}")
        print(f"  wall              {result.wall_seconds:.3f}s")
        print(f"  throughput        {result.changes_per_second:.2f} successful changes/s")
        print(f"  p50               {result.p50_ms:.2f}ms")
        print(f"  p95               {result.p95_ms:.2f}ms")
        print(f"  store             {result.store_bytes} bytes")
        print(f"  detail            {result.detail}")
        print()


def _trunks_detail(store: Path, scenario: Scenario) -> str:
    refs = list((store / "refs").rglob("*")) if (store / "refs").exists() else []
    ref_count = sum(1 for path in refs if path.is_file())
    return (
        f"shared local Trunks store, {ref_count} refs; "
        f"{'independent branches should all succeed' if scenario == 'branches' else 'same branch should allow one winner and reject stale CAS writers'}"
    )


def _payload(index: int, payload_bytes: int) -> bytes:
    prefix = f"agent={index}\n".encode("utf-8")
    if payload_bytes <= len(prefix):
        return prefix[:payload_bytes]
    return prefix + (b"x" * (payload_bytes - len(prefix)))


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _run(args: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check)


if __name__ == "__main__":
    raise SystemExit(main())
