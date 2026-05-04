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
trunks storage create --name backup --mirror --url s3://company-trunks-backup
trunks storage ping primary
trunks storage list --json --limit 20 --offset 0
trunks storage get --name primary --json
trunks storage update --name primary --url s3://company-trunks-2 --json
trunks storage delete --name primary --json
```

Storage commands configure named backend profiles. Git remotes use those names with `trunks://<storage>/<repo>`. A primary profile plus explicit mirror profiles writes through a strict multi-backend path; inline `trunks+<scheme>://...` URLs target one backend.

## Git Remote Helper

```bash
git remote add origin trunks://primary/my-app
git push -u origin main
git clone trunks://primary/my-app ./my-app-copy
git remote add backup trunks+s3://company-trunks-backup/trunks/my-app.trunk
```

`git-remote-trunks` is installed with the Python package. Git invokes it automatically for `trunks://` and `trunks+...://` remotes. Real Git owns `.git`; Trunks does not install a global `git` wrapper for this path.

## Repos

```bash
trunks mount --repo my-app --path ./my-app
trunks mount --repo my-app --path ./my-app --watch
trunks mount --repo my-app --path ./my-app --storage primary
trunks mount --repo my-app --path ./my-app --no-git
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

`trunks mount` also bootstraps git: if no `.git` exists at the mount path it
runs `git init --initial-branch=main`, and if no `origin` remote is configured
it adds `origin -> trunks://<storage>/<repo>` using the single configured
primary storage profile. Pass `--storage <name>` to pick one explicitly when
multiple primaries exist; pass `--no-git` to skip the bootstrap entirely.
Existing non-trunks origins are preserved.

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

## Actions And Sandboxes

```bash
trunks actions run --command "python3.12 -m unittest" --json
trunks actions enqueue --command "python3.12 -m unittest" --timeout 1800 --artifact reports --json
trunks actions execute --id executor-1 --json
trunks actions list --status succeeded --json
trunks actions describe --id <run-id> --json
trunks actions status --id <run-id> --json
trunks actions logs --id <run-id>
trunks actions watch --id <run-id>
trunks actions cancel --id <run-id> --json
trunks actions artifacts --id <run-id> --json
trunks actions secrets bind NPM_TOKEN env:NPM_TOKEN
trunks actions capacity --json
trunks actions health --json
trunks actions oidc token --id <run-id> --audience aws --json
trunks actions prune --older-than 30d --keep-last 100 --dry-run --json
trunks actions workflows lint --json
trunks actions workflow-runs --json

trunks sandboxes providers --json
trunks sandboxes providers show --name local --json
trunks sandboxes providers test --name local
trunks sandboxes providers orphans --name daytona --json
trunks sandboxes providers cleanup --name daytona --dry-run --json
trunks sandboxes providers orphans --name digitalocean --json
trunks sandboxes providers cleanup --name digitalocean --dry-run --json
trunks sandboxes providers scaffold --name acme-fast -o ./providers
trunks sandboxes specs --provider local
trunks sandboxes regions --provider local
```

Actions state is stored in Trunks refs and objects. Sandbox providers are async runtimes that execute jobs for Actions.

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
