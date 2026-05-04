# Actions Examples

These examples show how to use Trunks Actions as a headless CI runtime from Python.

Normal CI starts from `git push`. These examples are for apps and agents that want to create work directly: queue a job, run it on a sandbox provider, watch logs, and exit with the result.

## Headless CI Runner

`headless-ci-runner.py` queues one job in a Trunks repo, runs one executor, streams live logs from the watch API, and exits with the action result.

From a Trunks repo:

```bash
python examples/actions/headless-ci-runner.py --command "python3.12 -m unittest discover"
```

From another directory:

```bash
python examples/actions/headless-ci-runner.py --repo /path/to/repo --command "npm test"
```

The example uses:

- `enqueue_command(...)` to write a pending run
- `execute_once(...)` to claim and execute the run through a sandbox provider
- `watch_run_async(...)` to stream state and logs without an Actions server

## Retention

Run cleanup outside request handling:

```bash
trunks actions prune --older-than 30d --keep-last 100 --json
trunks check --clean
```

`trunks actions prune` removes terminal Actions refs. `trunks check --clean` removes unreferenced objects after refs are gone.
