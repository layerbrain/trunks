# Daytona + S3-Compatible Storage

This example shows Trunks Actions running CI from a normal `git push`.

A ready-to-run example repo lives at
[`docs/actions/examples/daytona-minio/`](../../../docs/actions/examples/daytona-minio/).
It includes the app, test scripts, and workflow. You configure storage, add
Daytona, make a change, and push.

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

## 2. Configure Trunks Storage

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

For a hosted S3-compatible backend, keep the same command shape and change
`--bucket`, `--endpoint`, `--region`, `--access-key`, and `--secret-key`.

## 3. Mount The Example Repo

```bash
cd docs/actions/examples/daytona-minio
trunks mount --repo daytona-minio-demo
```

`trunks mount` initializes `.git` if missing and wires `origin ->
trunks://minio/daytona-minio-demo`. No `git init`, no `git remote add` — pass
`--storage minio` if you have multiple primary storage profiles.

## 4. Add Daytona

```bash
trunks sandboxes providers add \
  --name daytona \
  --type daytona \
  --secret api_key=<daytona-api-key>

trunks sandboxes providers test --name daytona --live
```

Provider credentials are stored in local Trunks config. They are not written to
the repo.

## 5. Commit And Push

The workflow, app, and scripts are already in the example directory. Stage,
commit, and push:

```bash
git add .
git commit -m "trigger CI"
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

## What's In The Example

| File | Purpose |
| --- | --- |
| `.trunks/workflows/live-demo.yml` | Workflow: boot app, smoke test 60s, upload artifacts |
| `app/server.py` | FastAPI app with `/`, `/healthz`, `/echo/{msg}`, `/work` |
| `scripts/run-preview.sh` | Installs deps, starts uvicorn, waits for ready, runs smoke |
| `scripts/smoke.py` | Hits endpoints for 60s, writes `report.json` with latency stats |

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
