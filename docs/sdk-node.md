# Node SDK

```bash
pip install trunks
npm install @layerbrain/trunks
```

The Node SDK gives JavaScript and TypeScript applications the same file, versioning, and resource APIs as the CLI.

## Files

```ts
import { Trunks } from "@layerbrain/trunks";

const trunks = new Trunks();
const fs = await trunks.mount({ repo: "my-app", path: "./my-app", watch: true });

await fs.pull();
await fs.write("task.md", "Fix auth\n");
console.log((await fs.read("task.md")).toString());
await fs.checkpoint("update auth");
await fs.push();
```

## File Methods

```ts
await fs.read("README.md");
await fs.write("task.md", "Fix auth\n");
await fs.list("src");
await fs.exists("pyproject.toml");
await fs.copy("a.txt", "b.txt");
await fs.move("b.txt", "archive/b.txt");
await fs.mkdir("archive");
await fs.remove("archive/b.txt");
```

## Versioning

```ts
await fs.checkpoint("update auth");
await fs.log();
await fs.status();
await fs.push();
await fs.pull();
await fs.fetch();
```

## Resources

```ts
const branches = await trunks.branches.list({ limit: 20, offset: 0 });
await trunks.branches.create({ name: "feature/auth", from: "main" });
```

See [Resources](resources.md) for the full surface.

## Local Shell

```ts
const shell = fs.localShell();
const result = await shell.exec("grep -R auth .");
console.log(result.stdout);
```

`localShell()` runs on the machine where the Node process runs. For untrusted code, run the Node process inside the sandbox or execute shell commands through the sandbox provider.
