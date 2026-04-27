# Postgres Backend

Postgres is a strong backend for shared repos because compare-and-swap is transactional.

## URL Form

```text
postgres://<user>:<password>@<host>:<port>/<database>/<trunk-path>
```

## Credentials

The DSN is the credential. In CI or sandboxes, put it in an env var:

```bash
export TRUNKS_REPO=postgres://user:password@host/db/trunks/myrepo.trunk
```

## Connect And Validate

```bash
export TRUNKS_STORAGE_PRIMARY_DSN=postgres://user:password@host/db
trunks storage add --name primary --backend postgres --dsn-env TRUNKS_STORAGE_PRIMARY_DSN
trunks storage ping primary
```

The saved profile stores the DSN env-var name, not the password-bearing DSN. `--dsn` is accepted only for one-time validation when paired with `--dsn-env`; it is not persisted.

## Permissions

The database user needs permission to create/use Trunks tables and update rows.
