# Local Disk And Shared Filesystems

These schemes write to a directory: `local`, `file`, `nfs`, and `smb`.

## URL Forms

```text
local:///srv/trunks
file:///mnt/share
nfs:///mnt/nfs
smb:///mnt/smb
```

The backend is the directory. Each repo gets a `.trunk` directory under it:

```text
/srv/trunks/trunks/lazy-lms.trunk/
```

## Credentials

None in Trunks. The OS handles auth at mount time.

## Connect And Validate

```bash
trunks storage add --name primary --backend local --path /srv/trunks
trunks storage ping primary
```

## Notes

Network filesystems vary in locking and fsync behavior. For heavy concurrent agents, prefer S3-compatible storage or Postgres.
