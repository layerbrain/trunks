# Pydantic AI PR Agent

Typed Pydantic AI tools for a Trunks-mounted repo.

## Install

```bash
python3 -m pip install pydantic-ai trunks
```

## Configure

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=openai:gpt-4o-mini
export TRUNKS_REPO=my-app
export TRUNKS_PATH=/tmp/my-app
export TRUNKS_BACKEND=s3://company-agent-repos
```

## Run

```bash
python agent.py "Fix the auth test, run the focused test, inspect the diff, checkpoint, and push."
```

## Flow

```text
pull -> list/read -> write -> shell -> diff -> checkpoint -> push
```
