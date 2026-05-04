# Secrets

Trunks doesn't store secret values. It stores a binding: a name and where to read it from at run time.

## Bind

```bash
trunks actions secrets bind NPM_TOKEN env:NPM_TOKEN
trunks actions secrets list --json
trunks actions secrets show NPM_TOKEN --json
trunks actions secrets remove NPM_TOKEN --json
```

The binding `env:NPM_TOKEN` means "at execution time, read the executor's `NPM_TOKEN` environment variable." That value is injected into the sandbox as `NPM_TOKEN`. The plaintext is never written to refs, never returned by the CLI, never printed in `describe` or `show`.

## Use

In a workflow:

```yaml
steps:
  - run: npm publish
    env:
      NPM_TOKEN: ${{ secrets.NPM_TOKEN }}
```

`${{ secrets.NPM_TOKEN }}` resolves to the bound value at execution time. If `NPM_TOKEN` isn't bound, the run fails closed before the sandbox boots.

## Masking

Resolved secrets are masked in stored logs. Anything matching a secret value is replaced with `***` before the log chunk is written. Names with parts like `SECRET`, `TOKEN`, `PASSWORD`, `KEY`, `CREDENTIAL`, or `PASS` are also treated as sensitive and masked when their values appear in stdout/stderr.

Masking is best-effort. A workload that base64-encodes a secret before printing it bypasses masking.

## Names

Secret names are letters, numbers, and underscores only. No dashes, no dots, no spaces.

## Scope

Bindings are per-repo. `env:` reads the executor's environment at execution time; it doesn't encrypt or rotate the underlying source. Bindings can point at any source the resolver supports.
