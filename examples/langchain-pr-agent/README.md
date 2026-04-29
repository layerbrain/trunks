# LangChain / LangGraph PR Agent

LangChain tools for a Trunks-mounted repo.

## Install

```bash
python3 -m pip install langchain openai trunks
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

## Tools

- `pull`
- `list_files`
- `read`
- `write`
- `shell`
- `diff`
- `checkpoint`
- `push`

For human review, use:

```text
pull -> read/write/shell -> diff -> review -> checkpoint -> push
```
