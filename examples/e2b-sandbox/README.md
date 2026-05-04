# E2B Sandbox

Mount Trunks inside E2B so repo state survives sandbox destruction.

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

`trunks mount` initializes `.git` and wires `origin -> trunks://primary/$TRUNKS_REPO` automatically.

## Save

```bash
trunks diff --vs main
git add .
git commit -m "agent output"
git push
```

Another sandbox or developer machine can mount the same repo and pull the checkpoint.
