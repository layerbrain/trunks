# Postgres

Postgres is useful when you want transactional compare-and-swap refs.

```bash
export TRUNKS_STORAGE_PRIMARY_DSN=postgres://user:password@host/db

trunks storage create --name primary \
  --backend postgres \
  --dsn-env TRUNKS_STORAGE_PRIMARY_DSN

trunks storage ping primary
```

The saved profile stores the env-var name, not the DSN value.

The database user needs permission to create and update Trunks tables.
