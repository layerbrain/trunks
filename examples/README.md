# Examples

Each example exposes the same Trunks tools:

- `pull`
- `list`
- `read`
- `write`
- `shell`
- `diff`
- `checkpoint`
- `push`

## Agent Framework Examples

| Framework | Directory |
|---|---|
| Mastra | [mastra-pr-agent](mastra-pr-agent/README.md) |
| OpenAI Agents SDK | [openai-agents-pr-agent](openai-agents-pr-agent/README.md) |
| LangChain / LangGraph | [langchain-pr-agent](langchain-pr-agent/README.md) |
| CrewAI | [crewai-pr-agent](crewai-pr-agent/README.md) |
| Pydantic AI | [pydantic-ai-pr-agent](pydantic-ai-pr-agent/README.md) |
| Agno | [agno-pr-agent](agno-pr-agent/README.md) |

## Sandboxes

| Sandbox | Directory |
|---|---|
| E2B | [e2b-sandbox](e2b-sandbox/README.md) |
| Daytona | [daytona-sandbox](daytona-sandbox/README.md) |
| DigitalOcean | [digitalocean-sandbox](digitalocean-sandbox/README.md) |
| Blaxel | [blaxel-sandbox](blaxel-sandbox/README.md) |

Mount Trunks where commands execute. If shell commands run in a sandbox, mount inside the sandbox.

## Actions

| Example | Directory |
|---|---|
| Headless CI runner | [actions](actions/README.md) |

Normal CI starts from `git push`. The Actions example is for apps and agents that want to queue work directly.
