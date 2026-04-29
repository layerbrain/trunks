# Daytona Sandbox

Mount Trunks inside a Daytona workspace when repo state must move across Daytona, local development, CI, and other sandboxes.

## Bootstrap

```bash
python3 -m pip install trunks

export TRUNKS_REPO=my-app
export TRUNKS_BACKEND=s3://company-agent-repos
export TRUNKS_PATH=/workspace/my-app

trunks mount --repo "$TRUNKS_REPO" --path "$TRUNKS_PATH" --backend "$TRUNKS_BACKEND" --watch
cd "$TRUNKS_PATH"
trunks pull
```

## Save

```bash
trunks diff --vs main
trunks checkpoint -m "agent output"
trunks push
```
