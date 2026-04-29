# @layerbrain/trunks

Node SDK for Trunks.

Install the CLI where commands run, then install the Node package:

```bash
pip install trunks
npm install @layerbrain/trunks
```

```ts
import { Trunks } from "@layerbrain/trunks";

const trunks = new Trunks();
const fs = await trunks.mount({ repo: "my-app", path: "./my-app", watch: true });

await fs.write("task.md", "Fix auth\n");
await fs.checkpoint("agent output");
await fs.push();
```

## Scripts

```bash
npm run typecheck
npm test
npm run pack:dry
npm run publish:dry
```
