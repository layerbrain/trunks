# Azure Blob Storage

```bash
trunks storage create --name primary \
  --backend azure \
  --account-name myaccount \
  --container my-container

trunks storage ping primary
```

URL form:

```text
azure://<account>/<container>
```

Credential options:

```bash
export AZURE_STORAGE_KEY=...
export AZURE_STORAGE_SAS_TOKEN=...
```

The credential needs blob read, write, delete, and list permissions on the container or prefix.
