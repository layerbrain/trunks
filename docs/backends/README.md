# Backends

A backend is one storage root. Trunks puts every repo under it by name.

```text
repo:    my-app
storage: s3://company-trunks
actual:  s3://company-trunks/trunks/my-app.trunk
```

You configure the storage root once. Application code passes repo names, not URLs.

## Supported Backends

| Backend | Storage root | Guide |
|---|---|---|
| S3-compatible storage | `s3://<bucket>` | [s3.md](s3.md) |
| Azure Blob Storage | `azure://<account>/<container>` | [azure.md](azure.md) |
| Google Cloud Storage | `gcs://<bucket>` | [gcs.md](gcs.md) |
| Postgres | `postgres://.../<database>` | [postgres.md](postgres.md) |
| SFTP | `sftp://<user>@<host>/<path>` | [sftp.md](sftp.md) |
| Local disk / NFS / SMB | `local:///path` | [local.md](local.md) |
| Memory | `memory://` | [memory.md](memory.md) |

S3-compatible covers AWS S3, R2, Tigris, MinIO, B2, Wasabi, Spaces, Ceph, NetApp.

## Picking One

| Use case | Backend |
|---|---|
| Production concurrent workloads | S3-compatible storage or Postgres |
| Self-hosted object storage | MinIO |
| Strong transactional refs | Postgres |
| Local development | `local:///tmp/trunks-store` |
| Existing NAS | `local:///mnt/share` |
| Unit tests | `memory://` |

For high-concurrency writes, prefer S3-compatible storage or Postgres. Both pass the same multi-writer CAS conflict test.
