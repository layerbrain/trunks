# OpenAI Agents SDK PR Agent

Function tools for a Trunks-mounted repo.

## Install

```bash
python3 -m pip install openai-agents trunks
```

## Configure

```bash
export OPENAI_API_KEY=...
export TRUNKS_REPO=my-app
export TRUNKS_PATH=/tmp/my-app
export TRUNKS_BACKEND=s3://company-agent-repos
```

Local storage:

```bash
export TRUNKS_BACKEND=local:///tmp/trunks-store
```

## Run

```bash
python agent.py "Fix the auth test, run the focused test, inspect the diff, checkpoint, and push."
```

## Tools

- `pull`
- `list_files`
- `read`
- `write`
- `shell`
- `diff`
- `checkpoint`
- `push`

Run this process inside the sandbox when shell commands must be sandboxed.
