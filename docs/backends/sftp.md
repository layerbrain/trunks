# SFTP Backend

## URL Form

```text
sftp://<user>@<host>:<port>/<path>
```

## Credentials

Trunks uses SSH key auth or a password from an environment variable.

```bash
export SFTP_PASSWORD=...
```

## Connect And Validate

```bash
trunks storage add --name primary --backend sftp \
  --host trunks.example.com \
  --user deploy \
  --path /srv/trunks \
  --password-env SFTP_PASSWORD
trunks storage ping primary
```

For key auth, use `--ssh-key ~/.ssh/id_ed25519` and optionally `--key-passphrase ...`. If you pass `--password`, it is stored only in the local `.trunks/<repo>.trunk` database and never written to the remote backend. `--password-env` stores only the env-var name.

## Notes

SFTP is fine for simple storage and backups. For many concurrent writers, prefer S3-compatible storage or Postgres.
