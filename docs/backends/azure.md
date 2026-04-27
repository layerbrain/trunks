# Azure Blob Storage Backend

## URL Form

```text
azure://<account>/<container>
azure://<account>/<container>/<name>.trunk
```

The backend root is the container/prefix. The trunk is the repo directory inside it.

## Credentials

Set one of:

```bash
export AZURE_STORAGE_KEY=...
export AZURE_STORAGE_SAS_TOKEN=...
```

## Connect And Validate

```bash
trunks storage add --name primary --backend azure \
  --account-name myaccount \
  --container my-container
trunks storage ping primary
```

The saved profile stores account, container, prefix, endpoint, role, and any inline credentials you pass. Credentials are local-only in `.trunks/<repo>.trunk`; the remote backend receives only non-secret storage settings.

## Permissions

The credential needs read, write, delete, and list permissions for blobs in the container.
