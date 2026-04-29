# SFTP

```bash
export SFTP_PASSWORD=...

trunks storage create --name primary \
  --backend sftp \
  --host trunks.example.com \
  --user deploy \
  --path /srv/trunks \
  --password-env SFTP_PASSWORD

trunks storage ping primary
```

Key auth:

```bash
trunks storage create --name primary \
  --backend sftp \
  --host trunks.example.com \
  --user deploy \
  --path /srv/trunks \
  --ssh-key ~/.ssh/id_ed25519
```

Use SFTP for simple remote storage and backups. For many concurrent writers, prefer S3-compatible storage or Postgres.
