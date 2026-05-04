# Daytona + S3-Compatible Storage

This example runs a Trunks Actions job on Daytona and stores the run state,
logs, and artifacts in S3-compatible storage. Use MinIO locally for a demo, or
replace it with S3, R2, Tigris, Spaces, Ceph, or another compatible backend.

The job itself can be any shell command. The example below boots a small web app,
sends traffic to it, writes `report.json`, uploads `logs/app.log`, and destroys
the sandbox.

## 1. Start MinIO Locally

```bash
docker run -d --rm \
  --name trunks-demo-minio \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=minioadmin \
  -p 9000:9000 \
  -p 9001:9001 \
  minio/minio:latest \
  server /data --console-address :9001
```

Open the MinIO console:

```text
http://127.0.0.1:9001
```

Login:

```text
minioadmin / minioadmin
```

## 2. Configure Trunks Storage

From your repo:

```bash
trunks storage add \
  --name minio \
  --backend s3 \
  --bucket trunks-demo \
  --endpoint http://127.0.0.1:9000 \
  --region us-east-1 \
  --access-key minioadmin \
  --secret-key minioadmin \
  --json

trunks storage ping --name minio
```

Expected result:

```text
minio      ok      s3://trunks-demo/trunks/<repo>.trunk
```

For a hosted S3-compatible backend, keep the same command shape and change
`--bucket`, `--endpoint`, `--region`, `--access-key`, and `--secret-key`.

## 3. Add Daytona

```bash
trunks sandboxes providers add \
  --name daytona \
  --type daytona \
  --secret api_key=<daytona-api-key>

trunks sandboxes providers test --name daytona --live --json
```

Provider credentials are stored in local Trunks config. They are not written to
the repo.

## 4. Add A Demo Job

Create `scripts/smoke.py`:

```python
import json
import statistics
import time
import urllib.request

URL = "http://127.0.0.1:8080"
DURATION_S = 60

latencies = []
statuses = {}
errors = 0
started = time.time()

while time.time() - started < DURATION_S:
    try:
        begin = time.time()
        with urllib.request.urlopen(URL + "/healthz", timeout=5) as response:
            response.read()
            statuses[response.status] = statuses.get(response.status, 0) + 1
        latencies.append((time.time() - begin) * 1000)
    except Exception:
        errors += 1
    time.sleep(0.25)

latencies.sort()
report = {
    "target": URL,
    "duration_s": DURATION_S,
    "requests": len(latencies),
    "errors": errors,
    "statuses": statuses,
    "latency_ms": {
        "min": round(min(latencies), 2),
        "p50": round(statistics.median(latencies), 2),
        "p95": round(latencies[int(len(latencies) * 0.95)], 2),
        "max": round(max(latencies), 2),
    },
}

with open("report.json", "w") as handle:
    json.dump(report, handle, indent=2)

print(json.dumps(report, indent=2))
raise SystemExit(0 if errors == 0 else 1)
```

Create `scripts/run-preview.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

python3 -m pip install --quiet --no-input fastapi 'uvicorn[standard]'

mkdir -p logs
cat > app.py <<'PY'
from fastapi import FastAPI

app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"ok": True}
PY

python3 -m uvicorn app:app --host 0.0.0.0 --port 8080 > logs/app.log 2>&1 &
APP_PID=$!

cleanup() {
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break
  sleep 0.5
done

python3 scripts/smoke.py
```

## 5. Run On Daytona

```bash
trunks actions run \
  --command 'bash scripts/run-preview.sh' \
  --provider daytona \
  --strict-provider \
  --isolation container \
  --arch x86_64 \
  --cpu 2 \
  --memory 4gb \
  --disk 8gb \
  --timeout 240 \
  --artifact report.json \
  --artifact logs/app.log \
  --json
```

The command returns a JSON payload with an `id`. If you missed it:

```bash
trunks actions list --limit 5
```

## 6. Watch Logs And Download Artifacts

```bash
trunks actions watch --id <run-id>
trunks actions logs --id <run-id>
trunks actions artifacts --id <run-id>
```

Download the latency report:

```bash
trunks actions artifacts --id <run-id> get report.json -o report.json
cat report.json
```

Download the app log:

```bash
trunks actions artifacts --id <run-id> get logs/app.log -o app.log
```

## 7. Show The Storage Layout

In MinIO, browse:

```text
trunks-demo/
  trunks/
    <repo>.trunk/
      objects/
      refs/
      journals/
```

Important paths:

```text
refs/actions/repos/<repo>/runs/<run-id>/state
refs/actions/repos/<repo>/runs/<run-id>/logs/1/
refs/actions/repos/<repo>/runs/<run-id>/artifacts/report.json
refs/actions/repos/<repo>/runs/<run-id>/artifacts/logs/app.log
objects/<sha-prefix>/<sha>
```

Artifacts are Trunks objects. The artifact refs point at immutable object ids.
Users retrieve them through Trunks:

```bash
trunks actions artifacts --id <run-id> get report.json -o report.json
```

If an artifact ref exists but the object was deleted or corrupted, retrieval
fails loudly. That is intentional: the ref says the artifact should exist, and a
missing blob means storage integrity was broken.

## 8. Cleanup

Check Daytona resources:

```bash
trunks sandboxes providers orphans --name daytona --json
```

Stop local MinIO:

```bash
docker rm -f trunks-demo-minio
```
