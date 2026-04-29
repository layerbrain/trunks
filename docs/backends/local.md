# Local Disk And Shared Filesystems

Use this for local development, mounted NAS, NFS shares, SMB shares, and simple self-hosting.

```bash
trunks storage create --name primary local:///srv/trunks
trunks storage ping primary
```

Repo path:

```text
/srv/trunks/trunks/my-app.trunk/
```

Supported URL forms:

```text
local:///srv/trunks
file:///mnt/share
nfs:///mnt/nfs
smb:///mnt/smb
```

The OS handles filesystem credentials and mount permissions.

For high-concurrency workloads, prefer S3-compatible storage or Postgres.
