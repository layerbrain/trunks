# Actions Examples

The Actions examples live under `examples/actions`.

Use these when you want to drive Actions from an app or script. Normal CI starts from `git push`; these examples show the lower-level queue, execute, and watch calls.

## Headless CI Runner

`examples/actions/headless-ci-runner.py` is the smallest working app integration:

```bash
python examples/actions/headless-ci-runner.py --repo /path/to/repo --command "python3.12 -m unittest discover"
```

It queues a run, starts one executor, streams live logs through the watch API, and exits with the job result. It is useful for app integrations, local demos, and proving the lifecycle without running a server.

The example uses the same Actions queue, leases, logs, and watch API as workflow-triggered runs. In normal CI usage, `.trunks/workflows/*.yml` files with `on: push` start automatically when `git push` goes through the Trunks git shim. For multiple hosts, point every executor at the same configured Trunks storage backend so they share `refs/actions/repos/<repo>/...`.

## Daytona + MinIO Live Demo

See [Daytona + S3-compatible storage](examples/daytona-minio.md).

This is the concrete demo for remote compute plus storage-backed Actions:
Daytona runs the job, MinIO or any S3-compatible backend stores the Trunks repo,
and Trunks retrieves logs/artifacts by run id.
