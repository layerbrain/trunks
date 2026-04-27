# Backends

Trunks works against any of these. Pick whatever you already have.

| Backend                    | URL                                  | Page                              |
|----------------------------|--------------------------------------|-----------------------------------|
| AWS S3                     | `s3://<bucket>/<prefix>`             | [s3.md](s3.md)                    |
| Cloudflare R2              | `r2://<bucket>/<prefix>`             | [s3.md](s3.md)                    |
| Tigris                     | `tigris://<bucket>/<prefix>`         | [s3.md](s3.md)                    |
| MinIO                      | `minio://<host>/<bucket>/<prefix>`   | [s3.md](s3.md)                    |
| Backblaze B2               | `b2://<bucket>/<prefix>`             | [s3.md](s3.md)                    |
| Wasabi                     | `wasabi://<bucket>/<prefix>`         | [s3.md](s3.md)                    |
| DigitalOcean Spaces        | `spaces://<bucket>/<prefix>`         | [s3.md](s3.md)                    |
| Ceph / NetApp (S3 API)     | `ceph://<bucket>` / `netapp://...`   | [s3.md](s3.md)                    |
| Azure Blob Storage         | `azure://<account>/<container>`      | [azure.md](azure.md)              |
| Google Cloud Storage       | `gcs://<bucket>/<prefix>`            | [gcs.md](gcs.md)                  |
| SFTP                       | `sftp://<user>@<host>/<path>`        | [sftp.md](sftp.md)                |
| Postgres                   | `postgres://...`                     | [postgres.md](postgres.md)        |
| Local directory / NFS / SMB| `local:///path` `file:///mnt/...`    | [local.md](local.md)              |
| Single SQLite file         | `/path/to/file.trunk`                | [local.md](local.md)              |
| Memory (tests only)        | `memory://`                          | [memory.md](memory.md)            |

## How to pick

- **You already have an S3 bucket** — use it. S3 is the most-tested backend and CAS is real.
- **You're running self-hosted** — MinIO is a single binary and behaves like S3.
- **You want strong consistency, no eventual surprises, real CAS** — use Postgres.
- **You've got a Linux server with a big disk** — use `local:///path`. Backups are just `tar` / `rsync`.
- **You're on a corporate network with NAS** — use `file:///mnt/share`. CAS is best-effort; don't point hundreds of agents at one branch.
- **You're writing tests** — use `memory://`.

## Capabilities at a glance

| Backend     | CAS refs | Locks | Journal | List | Strong reads |
|-------------|----------|-------|---------|------|--------------|
| S3 family   | yes      | yes   | yes     | yes  | yes          |
| Azure       | yes      | yes   | yes     | yes  | yes          |
| GCS         | yes      | yes   | yes     | yes  | yes          |
| SFTP        | yes*     | yes*  | yes     | yes  | yes          |
| Postgres    | yes      | no    | yes     | yes  | yes          |
| Local / FS  | yes*     | no    | yes     | yes  | yes          |
| Memory      | yes      | no    | yes     | yes  | yes          |

`yes*` means emulated, not native. Fine for one or a few writers. Use Postgres or an S3 if you need real CAS under heavy concurrent contention.
