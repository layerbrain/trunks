# API-Shaped Resources

CLI, Python, and Node return the same resource shape.

## List Envelope

```json
{
  "object": "list",
  "data": [],
  "limit": 20,
  "offset": 0,
  "total_count": 0,
  "has_more": false
}
```

## Resources

| Resource | Object |
|---|---|
| Repo | `repo` |
| Branch | `branch` |
| Tag | `tag` |
| Storage target | `storage_target` |
| Webhook | `webhook` |
| Audit event | `audit_event` |

## CLI

```bash
trunks repo create --name my-app --backend s3://company-trunks --json
trunks repo get --name my-app --json
trunks repo update --name my-app --backend s3://company-trunks-2 --json
trunks repo delete --name my-app --json
trunks repo list --json --limit 20 --offset 0

trunks branch create --name agent/run-7 --from main --json
trunks branch get --name agent/run-7 --json
trunks branch update --name agent/run-7 --from main --json
trunks branch delete --name agent/run-7 --json
trunks branch list --json --limit 20 --offset 0

trunks tag create --name v1.0 --at main --json
trunks tag get --name v1.0 --json
trunks tag update --name v1.0 --at main --json
trunks tag delete --name v1.0 --json
trunks tag list --json --limit 20 --offset 0

trunks storage create --name primary --url s3://company-trunks --json
trunks storage get --name primary --json
trunks storage update --name primary --url s3://company-trunks-2 --json
trunks storage delete --name primary --json
trunks storage list --json --limit 20 --offset 0

trunks webhook create https://example.com/trunks --on push --json
trunks webhook get <webhook-id> --json
trunks webhook update <webhook-id> --url https://example.com/trunks-2 --json
trunks webhook delete <webhook-id> --json
trunks webhook list --json --limit 20 --offset 0

trunks audit list --json --limit 20 --offset 0
```

## Python

```python
from trunks import Trunks

client = Trunks(cwd="./my-app")
repos = client.repos.list(limit=20, offset=0)
repo = client.repos.create(name="my-app", backend="s3://company-trunks")
repo = client.repos.get(name="my-app")
repo = client.repos.update(name="my-app", backend="s3://company-trunks-2")
client.repos.delete(name="my-app")

branch = client.branches.create(name="agent/run-7", from_ref="main")
branch = client.branches.get(name="agent/run-7")
branch = client.branches.update(name="agent/run-7", from_ref="main")
client.branches.delete(name="agent/run-7")

hook = client.webhooks.create(url="https://example.com/trunks", events=["push"])
hook = client.webhooks.get(id=hook["id"])
hook = client.webhooks.update(id=hook["id"], url="https://example.com/trunks-2")
client.webhooks.delete(id=hook["id"])
```

## Node

```ts
import { Trunks } from "@layerbrain/trunks";

const trunks = new Trunks({ cwd: "./my-app" });
const repos = await trunks.repos.list({ limit: 20, offset: 0 });
await trunks.repos.create({ name: "my-app", backend: "s3://company-trunks" });
await trunks.repos.get({ name: "my-app" });
await trunks.repos.update({ name: "my-app", backend: "s3://company-trunks-2" });
await trunks.repos.delete({ name: "my-app" });

const branch = await trunks.branches.create({ name: "agent/run-7", from: "main" });
await trunks.branches.get({ name: "agent/run-7" });
await trunks.branches.update({ name: "agent/run-7", from: "main" });
await trunks.branches.delete({ name: "agent/run-7" });

const hook = await trunks.webhooks.create({ url: "https://example.com/trunks", events: ["push"] });
await trunks.webhooks.get({ id: hook.id });
await trunks.webhooks.update({ id: hook.id, url: "https://example.com/trunks-2" });
await trunks.webhooks.delete({ id: hook.id });
```

## Audit

Audit events are local append-only records for mounts, checkpoints, branch changes, push/pull, and webhook delivery. Use them for product dashboards, debugging, billing, and compliance views.
