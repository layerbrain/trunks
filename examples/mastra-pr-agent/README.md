# Mastra PR Agent

Mastra tools for a Trunks-mounted repo.

## Install

```bash
npm install
```

For published apps, use:

```json
"@layerbrain/trunks": "^1.0.0"
```

## Configure

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=gpt-4o-mini
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
npm run build
npm run start -- "Fix the auth test, run the focused test, inspect the diff, checkpoint, and push."
```

## Tools

- `trunks_pull`
- `trunks_list`
- `trunks_read`
- `trunks_write`
- `trunks_shell`
- `trunks_diff`
- `trunks_checkpoint`
- `trunks_push`

Mount happens on first use:

```ts
const fs = await trunks.mount({
  repo: process.env.TRUNKS_REPO,
  path: process.env.TRUNKS_PATH,
  backend: process.env.TRUNKS_BACKEND,
  watch: true,
});
```

Run the Mastra process inside the sandbox when shell commands must be sandboxed.
