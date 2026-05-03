# Bring Your Own Sandbox Provider

Enterprises and compute vendors can add a sandbox provider without changing workflows.

## Python Providers

```bash
trunks sandboxes providers scaffold acme-fast -o ./providers
```

Providers are Python classes, even when the upstream provider is a normal REST API. The provider class owns auth, request bodies, response parsing, readiness polling, cleanup, and the static spec catalog it can satisfy. Trunks does not load provider SDK packages and does not route through JSON provider definitions.

HTTP-backed providers such as Daytona call the upstream REST API directly. VM-backed providers such as DigitalOcean and Prime Intellect use the upstream REST API for provisioning and SSH for execution. Both shapes implement the same `SandboxProvider` and `Sandbox` protocol.

## Register

External providers register through the `trunks.sandboxes.providers` entry-point group:

```toml
[project.entry-points."trunks.sandboxes.providers"]
acme-fast = "acme_fast:AcmeFastProvider"
```

## Implement

A provider must:

- expose `SandboxProvider.info`
- implement `discover`
- implement dynamic `supports`
- expose a bundled `Spec` catalog through `SandboxProvider.info.specs`
- create one `Sandbox` session per run
- hydrate the repo closure
- stream stdout/stderr log chunks with monotonic sequence numbers
- upload and download files without loading multi-GB payloads into memory
- cancel and destroy sessions

For remote hosted sandboxes, `hydrate` must make the requested commit or workspace closure available inside the sandbox before `run` starts. Do not claim a remote provider is Actions-ready until that path is tested against the real provider API.

## Prove It

```bash
trunks sandboxes providers test acme-fast --json
trunks sandboxes providers benchmark acme-fast --json
```

A provider should not be used for Actions routing until the live contract passes.
