# CrewAI PR Agent

CrewAI `BaseTool` classes for a Trunks-mounted repo.

## Install

```bash
python3 -m pip install crewai trunks
```

## Configure

```bash
export OPENAI_API_KEY=...
export TRUNKS_REPO=my-app
export TRUNKS_PATH=/tmp/my-app
export TRUNKS_BACKEND=s3://company-agent-repos
```

## Run

```bash
python agent.py "Fix the auth test, run the focused test, inspect the diff, checkpoint, and push."
```

## Tools

| Tool | Purpose |
|---|---|
| `trunks_pull` | Pull latest repo state |
| `trunks_list` | List directory entries |
| `trunks_read` | Read files |
| `trunks_write` | Write files |
| `trunks_shell` | Run commands in the mounted repo |
| `trunks_diff` | Show changed files |
| `trunks_checkpoint` | Commit finished changes |
| `trunks_push` | Push to storage |
