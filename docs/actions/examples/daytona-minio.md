# Daytona + S3-Compatible Storage

This example shows Trunks Actions running CI from a normal `git push`.

You add storage, add a sandbox provider, commit a workflow under
`.trunks/workflows`, and push. Trunks handles the workflow run without GitHub
Actions and without a central CI server.

Use MinIO locally for the demo, or replace it with S3, R2, Tigris, Spaces, Ceph,
or another S3-compatible backend.

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
  --secret-key minioadmin

trunks storage ping --name minio
```

Expected result:

```text
minio ok
```

For a hosted S3-compatible backend, keep the same command shape and change
`--bucket`, `--endpoint`, `--region`, `--access-key`, and `--secret-key`.

## 3. Add Daytona

```bash
trunks sandboxes providers add \
  --name daytona \
  --type daytona \
  --secret api_key=<daytona-api-key>

trunks sandboxes providers test --name daytona --live
```

Provider credentials are stored in local Trunks config. They are not written to
the repo.

## 4. Add The CI Files

Create `.trunks/workflows/live-demo.yml`:

```yaml
name: live-demo
on: [push, workflow_dispatch]

jobs:
  hello-trunks-ci:
    runs-on: ubuntu-latest
    env:
      PORT: "8080"
      DURATION_S: "60"
    steps:
      - uses: actions/checkout@v4

      - name: boot app + smoke
        run: bash scripts/run-preview.sh

      - name: upload report
        uses: actions/upload-artifact@v4
        with:
          name: latency-report
          path: |
            report.json
            logs/app.log
```

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

## 5. Commit And Push

Use Git normally:

```bash
git add .trunks/workflows/live-demo.yml scripts
git commit -m "Add Trunks CI"
git push
```

Trunks sees the push, creates the workflow run, routes the job to Daytona, writes
state to storage, uploads artifacts, and destroys the sandbox.

## 6. Watch Logs And Download Artifacts

Find the workflow run:

```bash
trunks actions workflow-runs --limit 5
```

Find the job run id:

```bash
trunks actions workflow-runs --id <workflow-run-id> jobs
```

Watch logs and inspect artifacts:

```bash
trunks actions watch --id <job-run-id>
trunks actions logs --id <job-run-id>
trunks actions artifacts --id <job-run-id>
```

Download the latency report:

```bash
trunks actions artifacts --id <job-run-id> get report.json -o report.json
cat report.json
```

Download the app log:

```bash
trunks actions artifacts --id <job-run-id> get logs/app.log -o app.log
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
refs/actions/repos/<repo>/workflow-runs/<workflow-run-id>/state
refs/actions/repos/<repo>/runs/<job-run-id>/state
refs/actions/repos/<repo>/runs/<job-run-id>/logs/1/
refs/actions/repos/<repo>/runs/<job-run-id>/artifacts/report.json
refs/actions/repos/<repo>/runs/<job-run-id>/artifacts/logs/app.log
objects/<sha-prefix>/<sha>
```

Artifacts are Trunks objects. The artifact refs point at immutable object ids.
Users retrieve them through Trunks:

```bash
trunks actions artifacts --id <job-run-id> get report.json -o report.json
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

## Manual Debug Command

Normal CI starts from `git push`. Use the direct command only when debugging one
job without a workflow trigger:

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
  --artifact logs/app.log
```
