# Blaxel Sandbox

Mount Trunks inside Blaxel when agents need multiple runtimes and durable repo state.

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
