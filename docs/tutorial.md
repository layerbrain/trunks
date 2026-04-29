# Tutorial

Five minutes from zero to a synced repo.

## 1. Install

```bash
pip install trunks
```

That ships the CLI. Add the Node package when your application runs in Node:

```bash
npm install @layerbrain/trunks
```

## 2. Pick A Backend

A backend is one storage root. Trunks puts every repo under it by name.

```text
repo:    my-app
storage: s3://company-trunks
actual:  s3://company-trunks/trunks/my-app.trunk
```

Production:

```bash
trunks repo create --name my-app --backend s3://company-trunks
```

Local for development:

```bash
trunks repo create --name my-app --backend local:///tmp/trunks-store
```

In-memory for tests:

```bash
trunks repo create --name my-app --backend memory://
```

See [Backends](backends/README.md) for S3, GCS, Azure, Postgres, SFTP, fileshares.

## 3. Mount

```bash
trunks mount --repo my-app --path ./my-app
cd ./my-app
```

You're in a normal folder now. `cat`, `vim`, `grep`, `npm`, `python` all work.

```bash
echo "Fix auth" > task.md
grep -R "login" src || true
```

## 4. Save A Version

```bash
trunks checkpoint -m "update auth"
trunks push
```

`checkpoint` writes a Git commit object. `push` syncs objects and refs to your backend. The two split exists because workloads can make a lot of small edits. Checkpoint locally as often as you want. Push when you're ready.

## 5. Continue Somewhere Else

```bash
trunks mount --repo my-app --path ./my-app
cd ./my-app
trunks pull
```

Same repo name. Same storage root. Same files, including the version you pushed.

## 6. Use Git If You Want

```bash
cd ./my-app
trunks
git checkout -b feature/auth
git add .
git commit -m "update auth"
git push
```

`trunks` opens a shell where Git writes through Trunks to your backend. Same commits. Same refs. No GitHub.

## Next

- [Lifecycle](lifecycle.md): what each command does to bytes
- [CLI reference](cli.md): every flag
- [Backends](backends/README.md): pick the right storage
- [Agents](agents.md): use Trunks with agent frameworks
