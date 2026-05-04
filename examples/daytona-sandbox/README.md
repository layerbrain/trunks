# Daytona Sandbox

Mount Trunks inside a Daytona workspace when repo state must move across Daytona, local development, CI, and other sandboxes.

Trunks also ships a Daytona sandbox provider for Actions. It is HTTP-backed, implemented as a Python provider class, and optional: connect it with `trunks sandboxes providers add daytona-main --type daytona --secret api_key=...`, then run `trunks sandboxes providers test daytona --live --json`. Live tests must leave no Daytona sandboxes behind. The built-in provider uses Daytona's default snapshot sandbox and does not expose arbitrary resource sizing as first-party support.

## Bootstrap

```bash
python3 -m pip install trunks

export TRUNKS_REPO=my-app
export TRUNKS_PATH=/workspace/my-app

trunks storage add --name primary --backend s3 --bucket company-agent-repos
trunks mount --repo "$TRUNKS_REPO" --path "$TRUNKS_PATH" --watch
cd "$TRUNKS_PATH"
trunks pull
```

`trunks mount` initializes `.git` if missing and wires `origin ->
trunks://primary/$TRUNKS_REPO` automatically, so the agent can `git push`
without any extra remote setup.

## Save

```bash
trunks diff --vs main
git add .
git commit -m "agent output"
git push
```
