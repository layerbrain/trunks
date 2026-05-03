# Agent Workflows

Agent frameworks don't need a special integration. They need eight tool names that map to eight Trunks verbs.

```text
pull        get the latest from the backend
list        what files are here
read        read a file
write       write a file
shell       run a shell command
diff        what's changed
checkpoint  save a version
push        sync to the backend
```

That's the whole agent surface. The examples below wire those eight verbs into popular frameworks. Copy the one for your stack.

## Examples

| Framework or sandbox | Example |
|---|---|
| Mastra | [examples/mastra-pr-agent](../examples/mastra-pr-agent/README.md) |
| OpenAI Agents SDK | [examples/openai-agents-pr-agent](../examples/openai-agents-pr-agent/README.md) |
| LangChain / LangGraph | [examples/langchain-pr-agent](../examples/langchain-pr-agent/README.md) |
| CrewAI | [examples/crewai-pr-agent](../examples/crewai-pr-agent/README.md) |
| Pydantic AI | [examples/pydantic-ai-pr-agent](../examples/pydantic-ai-pr-agent/README.md) |
| Agno | [examples/agno-pr-agent](../examples/agno-pr-agent/README.md) |
| E2B | [examples/e2b-sandbox](../examples/e2b-sandbox/README.md) |
| Daytona | [examples/daytona-sandbox](../examples/daytona-sandbox/README.md) |
| Blaxel | [examples/blaxel-sandbox](../examples/blaxel-sandbox/README.md) |

## CLI Shape

```bash
trunks mount --repo my-app --path ./my-app --watch
cd ./my-app

trunks pull
echo "Fix auth" > task.md
trunks diff --vs main
git add .
git commit -m "update auth"
git push
```

## Node Shape

```ts
import { Trunks } from "@layerbrain/trunks";

const fs = await new Trunks().mount({
  repo: "my-app",
  path: "/workspace/my-app",
  backend: process.env.TRUNKS_BACKEND,
  watch: true,
});

await fs.pull();
await fs.write("task.md", "Fix auth\n");
await fs.checkpoint("update auth");
await fs.push();
```

## The Sandbox Rule

Mount Trunks where commands execute.

If the agent process runs inside the sandbox, `fs.localShell()` runs inside the sandbox.

If the agent process runs on the host, mount Trunks inside the sandbox and run shell commands through the sandbox provider, like `e2b.runCommand` or `daytona.exec`.

Same files, same backend, same Git history. The shell just runs where the work happens.
