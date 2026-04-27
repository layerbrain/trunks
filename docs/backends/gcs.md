# Google Cloud Storage Backend

## URL Form

```text
gcs://<bucket>
gcs://<bucket>/<name>.trunk
```

## Credentials

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

## Connect And Validate

```bash
trunks storage add --name primary --backend gcs --bucket my-bucket
trunks storage ping primary
```

The saved profile stores bucket, prefix, endpoint/project when provided, and role. It does not store the service-account JSON.

## Permissions

The service account needs object create, get, delete, and list permissions on the bucket/prefix.
