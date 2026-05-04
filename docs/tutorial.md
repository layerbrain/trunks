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
trunks storage add primary --backend s3 --bucket company-trunks
```

Local for development:

```bash
trunks storage add primary --backend local --path /tmp/trunks-store
```

See [Backends](backends/README.md) for S3, GCS, Azure, Postgres, SFTP, fileshares.

## 3. Mount The Repo

```bash
mkdir my-app
cd my-app
trunks mount --repo my-app
```

`trunks mount` creates the Trunks repo, initializes `.git`, wires `origin -> trunks://primary/my-app`, and sets the current branch upstream. Real Git owns `.git`; Trunks only runs when Git invokes the remote helper for this URL.

## 4. Edit

```bash
echo "Fix auth" > task.md
grep -R "login" src || true
```

## 5. Save A Version

```bash
git add .
git commit -m "update auth"
git push
```

`git commit` is normal Git. `git push` invokes `git-remote-trunks`, uploads missing objects, advances the backend ref with compare-and-swap, and writes a durable push trigger ref for Trunks Actions.

## 6. Continue Somewhere Else

```bash
git clone trunks://primary/my-app ./my-app-copy
```

Same repo name. Same storage root. Same files, including the version you pushed.

## 7. Add A Mirror

```bash
trunks storage add backup --mirror --backend s3 --bucket company-trunks-backup
```

Pushes to `trunks://primary/my-app` now use a strict multi-backend path: the primary ref update is CAS-fenced, and configured mirrors receive the same objects, branch refs, and push trigger refs.

## Next

- [Lifecycle](lifecycle.md): what each command does to bytes
- [CLI reference](cli.md): every flag
- [Backends](backends/README.md): pick the right storage
- [Agents](agents.md): use Trunks with agent frameworks
