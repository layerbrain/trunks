import { openai } from "@ai-sdk/openai";
import { Agent } from "@mastra/core/agent";
import { trunksTools } from "./trunks.tools.js";

export const prAgent = new Agent({
  id: "trunks-pr-agent",
  name: "trunks-pr-agent",
  description: "A coding agent that edits a Trunks-backed repository and pushes checkpoints to user-owned storage.",
  instructions: [
    "You work inside a Trunks-backed repository.",
    "Pull before reading files.",
    "Use shell commands for search, tests, and build checks.",
    "Write focused changes, inspect the diff, checkpoint with a clear message, then push.",
    "Do not push if tests or type checks fail.",
  ].join(" "),
  model: openai(process.env.OPENAI_MODEL ?? "gpt-4o-mini"),
  tools: trunksTools,
});
