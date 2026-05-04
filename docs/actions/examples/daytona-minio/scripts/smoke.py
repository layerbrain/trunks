import json
import os
import statistics
import sys
import time
import urllib.request

URL = os.environ.get("TARGET_URL", "http://127.0.0.1:8080")
DURATION_S = int(os.environ.get("DURATION_S", "75"))
OUT = os.environ.get("REPORT_PATH", "report.json")


def hit(path: str) -> tuple[int, float]:
    started = time.time()
    req = urllib.request.Request(URL + path, headers={"User-Agent": "trunks-smoke/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
        code = resp.status
    return code, (time.time() - started) * 1000


def main() -> int:
    paths = ["/", "/healthz", "/echo/ping", "/work"]
    started = time.time()
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    errors = 0
    i = 0

    print(f"smoke target = {URL}")
    print(f"duration = {DURATION_S}s")
    sys.stdout.flush()

    while time.time() - started < DURATION_S:
        path = paths[i % len(paths)]
        i += 1
        try:
            code, ms = hit(path)
            latencies.append(ms)
            statuses[code] = statuses.get(code, 0) + 1
            if i % 25 == 0:
                print(f"  {i:5d} reqs  last={code} {ms:6.1f}ms  elapsed={int(time.time() - started)}s")
                sys.stdout.flush()
        except Exception as exc:
            errors += 1
            print(f"  err: {exc}")
            sys.stdout.flush()
        time.sleep(0.25)

    if not latencies:
        print("no successful requests")
        return 1

    latencies.sort()
    report = {
        "target": URL,
        "public_url": os.environ.get("PUBLIC_URL", ""),
        "duration_s": DURATION_S,
        "requests": len(latencies),
        "errors": errors,
        "statuses": statuses,
        "latency_ms": {
            "min": round(min(latencies), 2),
            "p50": round(statistics.median(latencies), 2),
            "p95": round(latencies[int(len(latencies) * 0.95)], 2),
            "p99": round(latencies[int(len(latencies) * 0.99)], 2),
            "max": round(max(latencies), 2),
            "mean": round(statistics.mean(latencies), 2),
        },
    }

    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)

    print()
    print("=" * 60)
    print(json.dumps(report, indent=2))
    print("=" * 60)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
