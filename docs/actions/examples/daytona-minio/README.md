# Daytona + MinIO Example

Run Trunks Actions CI on Daytona with S3-compatible storage. No GitHub Actions, no central CI server.

The code is already here. You just configure storage, add Daytona, and push.

## Prerequisites

- Docker (for MinIO)
- `pip install trunks`
- A Daytona API key

## 1. Start MinIO

```bash
docker run -d --rm \
  --name trunks-demo-minio \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=minioadmin \
  -p 9000:9000 -p 9001:9001 \
  minio/minio:latest \
  server /data --console-address :9001
```

## 2. Mount the repo

```bash
cd docs/actions/examples/daytona-minio
trunks mount --repo daytona-minio-demo
```

## 3. Add storage

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

For production, swap MinIO for S3, R2, Tigris, Spaces, or any S3-compatible backend.

## 4. Add Daytona

```bash
trunks sandboxes providers add \
  --name daytona \
  --type daytona \
  --secret api_key=<your-daytona-api-key>

trunks sandboxes providers test --name daytona --live
```

## 5. Push

```bash
git add .trunks/workflows/live-demo.yml app scripts README.md
git commit -m "Add Trunks CI"
git push
```

The `on: push` trigger fires automatically. Trunks routes the job to Daytona, runs it in a container, writes logs and artifacts to MinIO, and destroys the sandbox.

## 6. Watch

```bash
trunks actions workflow-runs --limit 5
trunks actions logs --id <job-run-id>
trunks actions artifacts --id <job-run-id>
trunks actions artifacts --id <job-run-id> get report.json -o report.json
cat report.json
```

## 7. Cleanup

```bash
docker rm -f trunks-demo-minio
```

## What's in this example

| File | Purpose |
| --- | --- |
| `.trunks/workflows/live-demo.yml` | Workflow: boot app, smoke test 60s, upload artifacts |
| `app/server.py` | FastAPI app with `/`, `/healthz`, `/echo/{msg}`, `/work` |
| `scripts/run-preview.sh` | Installs deps, starts uvicorn, waits for ready, runs smoke |
| `scripts/smoke.py` | Hits endpoints for 60s, writes `report.json` with latency stats |

## Trigger filters

The workflow uses `on: [push, workflow_dispatch]` which fires on every push. You can scope it:

```yaml
on:
  push:
    branches: [main]
    paths: [app/**, scripts/**]
```

This only triggers when pushing to `main` with changes in `app/` or `scripts/`.
