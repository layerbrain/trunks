# Daytona + MinIO Example

Run Trunks Actions CI on Daytona with S3-compatible storage. No GitHub Actions, no central CI server.

This example repo is ready to run. It includes the app, test scripts, and workflow. You configure storage, add Daytona, make a change, and push.

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

## 5. Make a change and push

```bash
echo "# my change" >> README.md
git add README.md
git commit -m "trigger CI"
git push
```

The `on: push` trigger fires automatically. Trunks routes the job to Daytona, runs it in a container, writes logs and artifacts to MinIO, and destroys the sandbox.

## 6. Watch

```bash
trunks actions workflow-runs --limit 5
trunks actions workflow-runs --id <workflow-run-id> jobs
trunks actions watch --id <job-run-id>
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
