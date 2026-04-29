# CLI Reference

## Version

```bash
trunks --version
```

## Storage

```bash
trunks storage wizard
trunks storage create --name primary --url s3://company-trunks
trunks storage create --name primary --url local:///tmp/trunks-store
trunks storage ping primary
trunks storage list --json --limit 20 --offset 0
trunks storage get --name primary --json
trunks storage update --name primary --url s3://company-trunks-2 --json
trunks storage delete --name primary --json
```

Storage commands configure where repo names sync.

## Repos

```bash
trunks mount --repo my-app --path ./my-app
trunks mount --repo my-app --path ./my-app --watch
trunks mount --repo big-repo --path ./big-repo --mode virtual
trunks unmount --path ./my-app
trunks repo create --name my-app --backend s3://company-trunks --json
trunks repo get --name my-app --json
trunks repo update --name my-app --backend s3://company-trunks-2 --json
trunks repo delete --name my-app --json
trunks repo list --json --limit 20 --offset 0
trunks status --path ./my-app --json
trunks doctor --path ./my-app --ping --json
```

Default mount uses a real directory. `--mode virtual` sparsely materializes large repos.

## Versioning

```bash
trunks diff
trunks diff --vs main --json
trunks checkpoint -m "update auth"
trunks history --json
trunks log --json
trunks push
trunks pull
trunks fetch
```

## Branches And Tags

```bash
trunks branch list --json --limit 20 --offset 0
trunks branch create --name feature/auth --from main
trunks branch get --name feature/auth --json
trunks branch update --name feature/auth --from main --json
trunks branch switch --name feature/auth
trunks branch delete --name feature/auth

trunks tag list --json --limit 20 --offset 0
trunks tag create --name v1.0 --at main
trunks tag get --name v1.0 --json
trunks tag update --name v1.0 --at main --json
trunks tag delete --name v1.0
trunks rollback --to tags/v1.0
```

Use branches for parallel work.

## Cache

```bash
trunks cache stats --json
trunks cache prune --max-size 5GB
trunks cache verify
trunks cache clear
```

## Webhooks And Audit

```bash
trunks webhook create https://example.com/trunks --on push --json
trunks webhook get <webhook-id> --json
trunks webhook update <webhook-id> --url https://example.com/trunks-2 --json
trunks webhook list --json --limit 20 --offset 0
trunks webhook delete <webhook-id> --json

trunks audit list --json --limit 20 --offset 0
```

Webhooks are signed with `X-Trunks-Signature: sha256=<hmac>`. Audit events are local append-only records for operators and product builders.
