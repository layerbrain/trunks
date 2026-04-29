# Google Cloud Storage

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

trunks storage create --name primary --backend gcs --bucket company-trunks
trunks storage ping primary
```

URL form:

```text
gcs://<bucket>
```

Repo path:

```text
gcs://company-trunks/trunks/my-app.trunk/
```

The service account needs object create, read, delete, and list permissions for the bucket or prefix.
