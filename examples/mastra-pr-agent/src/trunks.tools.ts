import { createTool } from "@mastra/core/tools";
import { Trunks, type TrunksFileSystem } from "@layerbrain/trunks";
import { z } from "zod";

const repo = process.env.TRUNKS_REPO ?? "my-app";
const repoPath = process.env.TRUNKS_PATH ?? `./${repo}`;
const backend = process.env.TRUNKS_BACKEND;

let mounted: Promise<TrunksFileSystem> | undefined;

async function getFilesystem(): Promise<TrunksFileSystem> {
  mounted ??= new Trunks({
    bin: process.env.TRUNKS_BIN ?? "trunks",
    rpcTimeoutMs: Number(process.env.TRUNKS_RPC_TIMEOUT_MS ?? 30_000),
  }).mount({
    repo,
    path: repoPath,
    backend,
    watch: true,
  });
  return mounted;
}

export const trunksPull = createTool({
  id: "trunks_pull",
  description: "Pull the latest repository state from the configured Trunks Git-compatible backend.",
  inputSchema: z.object({}),
  outputSchema: z.object({
    ok: z.boolean(),
  }),
  execute: async () => {
    const fs = await getFilesystem();
    await fs.pull();
    return { ok: true };
  },
});

export const trunksList = createTool({
  id: "trunks_list",
  description: "List files in the mounted Trunks repository.",
  inputSchema: z.object({
    path: z.string().default("."),
  }),
  outputSchema: z.object({
    entries: z.array(z.string()),
  }),
  execute: async (input) => {
    const fs = await getFilesystem();
    return { entries: await fs.list(input.path) };
  },
});

export const trunksRead = createTool({
  id: "trunks_read",
  description: "Read a UTF-8 file from the mounted Trunks repository.",
  inputSchema: z.object({
    path: z.string(),
  }),
  outputSchema: z.object({
    content: z.string(),
  }),
  execute: async (input) => {
    const fs = await getFilesystem();
    return { content: (await fs.read(input.path)).toString("utf8") };
  },
});

export const trunksWrite = createTool({
  id: "trunks_write",
  description: "Write a UTF-8 file into the mounted Trunks repository.",
  inputSchema: z.object({
    path: z.string(),
    content: z.string(),
  }),
  outputSchema: z.object({
    ok: z.boolean(),
  }),
  execute: async (input) => {
    const fs = await getFilesystem();
    await fs.write(input.path, input.content);
    return { ok: true };
  },
});

export const trunksShell = createTool({
  id: "trunks_shell",
  description: "Run a shell command in the mounted Trunks repository directory.",
  inputSchema: z.object({
    command: z.string(),
  }),
  outputSchema: z.object({
    stdout: z.string(),
    stderr: z.string(),
    exitCode: z.number(),
  }),
  execute: async (input) => {
    const fs = await getFilesystem();
    return fs.localShell().exec(input.command);
  },
});

export const trunksDiff = createTool({
  id: "trunks_diff",
  description: "Show changed file paths against a branch, tag, or commit.",
  inputSchema: z.object({
    vs: z.string().default("main"),
  }),
  outputSchema: z.object({
    files: z.array(
      z.object({
        status: z.enum(["A", "M", "D"]),
        path: z.string(),
      }),
    ),
  }),
  execute: async (input) => {
    const fs = await getFilesystem();
    return { files: await fs.diff(input.vs) };
  },
});

export const trunksCheckpoint = createTool({
  id: "trunks_checkpoint",
  description: "Create a Trunks checkpoint from current repository changes.",
  inputSchema: z.object({
    message: z.string(),
  }),
  outputSchema: z.object({
    ok: z.boolean(),
  }),
  execute: async (input) => {
    const fs = await getFilesystem();
    await fs.checkpoint(input.message);
    return { ok: true };
  },
});

export const trunksPush = createTool({
  id: "trunks_push",
  description: "Push committed repository state to the configured Trunks Git-compatible backend.",
  inputSchema: z.object({}),
  outputSchema: z.object({
    ok: z.boolean(),
  }),
  execute: async () => {
    const fs = await getFilesystem();
    await fs.push();
    return { ok: true };
  },
});

export const trunksTools = {
  trunksPull,
  trunksList,
  trunksRead,
  trunksWrite,
  trunksShell,
  trunksDiff,
  trunksCheckpoint,
  trunksPush,
};
