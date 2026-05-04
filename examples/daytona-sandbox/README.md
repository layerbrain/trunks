# Daytona Sandbox

Mount Trunks inside a Daytona workspace when repo state must move across Daytona, local development, CI, and other sandboxes.

Trunks also ships a Daytona sandbox provider for Actions. It is HTTP-backed, implemented as a Python provider class, and optional: connect it with `trunks sandboxes providers add daytona-main --type daytona --secret api_key=...`, then run `trunks sandboxes providers test daytona --live --json`. Live tests must leave no Daytona sandboxes behind. The built-in provider uses Daytona's default snapshot sandbox and does not expose arbitrary resource sizing as first-party support.

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
git add .
git commit -m "agent output"
git push
```
