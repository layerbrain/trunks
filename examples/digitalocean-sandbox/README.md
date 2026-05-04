# DigitalOcean Sandbox

Run Trunks Actions on small DigitalOcean Droplets when you want CI/CD over ordinary cloud compute.

DigitalOcean is a VM-backed provider, not a fast-start sandbox provider. Trunks creates a temporary SSH key, boots a tagged Ubuntu Droplet, uploads the workspace to `/root/trunks`, runs the command over SSH, downloads artifacts, and deletes the Droplet and SSH key.

```bash
trunks sandboxes providers add digitalocean-main \
  --type digitalocean \
  --secret api_key=... \
  --set region=nyc3

trunks sandboxes providers show digitalocean --json
trunks sandboxes providers test digitalocean --live --json

trunks actions run \
  --provider digitalocean \
  --strict-provider \
  --isolation vm \
  --arch x86_64 \
  --command "python3 -m compileall -q ." \
  --json

trunks sandboxes providers orphans digitalocean --json
trunks sandboxes providers cleanup digitalocean --dry-run --json
```

Optional settings:

```bash
export DIGITALOCEAN_REGION=nyc3
export DIGITALOCEAN_SIZE=s-1vcpu-1gb
export DIGITALOCEAN_IMAGE=ubuntu-24-04-x64
```

Use `cleanup` after interrupted live tests. It only targets resources whose names start with `trunks-`.
