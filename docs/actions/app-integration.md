# App Integration

The Trunks app can render Actions without an Actions server. It reads Trunks refs and objects.

## Runs

```text
refs/actions/repos/<repo>/runs/<run-id>/state
refs/actions/repos/<repo>/runs/<run-id>/logs/<attempt>/<seq>
refs/actions/repos/<repo>/runs/<run-id>/artifacts/<artifact-path>
refs/actions/repos/<repo>/index/runs/by-repo/<run-id>
refs/actions/repos/<repo>/index/runs/by-status/<phase>/<run-id>
refs/actions/repos/<repo>/queue/<spec-key>/<route-bucket>/<shard>/<run-id>
```

CLI equivalents:

```bash
trunks actions list --json
trunks actions describe --id <run-id> --json
trunks actions logs --id <run-id> --json
trunks actions artifacts --id <run-id> --json
```

## Workflow Runs

```text
refs/actions/repos/<repo>/workflow-runs/<workflow-run-id>/state
refs/actions/repos/<repo>/workflow-runs/<workflow-run-id>/jobs/<job>/<matrix-index>
refs/actions/repos/<repo>/index/workflow-runs/by-repo/<workflow-run-id>
refs/actions/repos/<repo>/index/workflow-runs/by-status/<phase>/<workflow-run-id>
```

CLI equivalents:

```bash
trunks actions workflow-runs --json
trunks actions workflow-runs --id <workflow-run-id> --json
trunks actions workflow-runs --id <workflow-run-id> jobs --json
trunks actions workflow-runs --id <workflow-run-id> graph --json
trunks actions workflow-runs --id <workflow-run-id> logs --json
```

Derived index refs are the dashboard read path. Do not scan every primary run ref on the hot path.
