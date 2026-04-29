import { prAgent } from "./agent.js";

const prompt =
  process.argv.slice(2).join(" ") ||
  "Pull the repo, inspect the current task, make the smallest safe change, run relevant checks, checkpoint, and push.";

const result = await prAgent.generate(prompt);
console.log(result.text);
