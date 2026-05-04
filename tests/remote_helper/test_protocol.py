from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trunks.backends.memory import Memory
from trunks.ids import ObjectId
from trunks.objects import parse_canonical_object
from trunks.remote_helper import Helper, _referenced_shas


def _real_git() -> str:
    for entry in ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if Path(entry).exists():
            return entry
    found = shutil.which("git")
    if not found:
        raise RuntimeError("git not on PATH")
    return found


GIT = _real_git()


def _git(cwd: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [GIT, "-C", str(cwd), *args],
        capture_output=True,
        input=input_bytes,
        check=True,
    )


def _make_repo(root: Path, *, files: dict[str, bytes], message: str = "init") -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--initial-branch=main", "-q")
    _git(root, "config", "user.email", "test@trunks.local")
    _git(root, "config", "user.name", "Trunks Test")
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-m", message, "-q")
    out = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    return out


class _FakeStdio:
    def __init__(self, payload: str) -> None:
        self._in = io.StringIO(payload)
        self._out = io.StringIO()

    def __enter__(self) -> "_FakeStdio":
        self._saved_in = sys.stdin
        self._saved_out = sys.stdout
        sys.stdin = self._in
        sys.stdout = self._out
        return self

    def __exit__(self, *exc: object) -> None:
        sys.stdin = self._saved_in
        sys.stdout = self._saved_out

    @property
    def output(self) -> str:
        return self._out.getvalue()


def _drive(backend: Memory, git_dir: Path, payload: str) -> str:
    helper = Helper(
        remote_name="origin",
        raw_url="trunks://test/repo",
        backend=backend,
        git_dir=git_dir,
        git_bin=GIT,
    )
    with _FakeStdio(payload) as stdio:
        asyncio.run(helper.loop())
        return stdio.output


class CapabilitiesTest(unittest.TestCase):
    def test_capabilities_emits_supported_commands(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _make_repo(root, files={"a.txt": b"hi\n"})
            output = _drive(backend, root / ".git", "capabilities\n\n")
        self.assertIn("fetch", output)
        self.assertIn("push", output)
        self.assertIn("option", output)
        self.assertTrue(output.endswith("\n\n"))


class EmptyListTest(unittest.TestCase):
    def test_list_with_no_refs_emits_blank_terminator(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _make_repo(root, files={"a.txt": b"hi\n"})
            output = _drive(backend, root / ".git", "list\n\n")
        self.assertEqual(output, "\n")


class PushAndListTest(unittest.TestCase):
    def test_push_writes_objects_and_ref_then_list_returns_them(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            sha = _make_repo(root, files={"a.txt": b"hello\n", "dir/b.txt": b"nested\n"})
            payload = (
                "push refs/heads/main:refs/heads/main\n"
                "\n"
                "list\n"
                "\n"
            )
            output = _drive(backend, root / ".git", payload)
        lines = output.split("\n")
        self.assertIn("ok refs/heads/main", lines)
        self.assertIn(f"{sha} refs/heads/main", lines)
        self.assertIn("@refs/heads/main HEAD", lines)
        ref = asyncio.run(backend.read_ref("refs/heads/main"))
        self.assertEqual(ref, ObjectId(sha))
        self.assertGreater(len(backend.objects), 0)

    def test_push_writes_storage_trigger_ref(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            sha = _make_repo(root, files={"a.txt": b"hello\n"})
            output = _drive(backend, root / ".git", "push refs/heads/main:refs/heads/main\n\n")
        self.assertIn("ok refs/heads/main", output)
        trigger_ref = f"refs/actions/repos/repo/triggers/push/{sha}"
        trigger_oid = asyncio.run(backend.read_ref(trigger_ref))
        self.assertIsNotNone(trigger_oid)
        assert trigger_oid is not None
        kind, payload = parse_canonical_object(backend.objects[trigger_oid])
        self.assertEqual(kind, "blob")
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data["object"], "push_trigger")
        self.assertEqual(data["commit"], sha)
        self.assertEqual(data["branch"], "main")
        self.assertEqual(data["ref"], "refs/heads/main")


class PushIgnoresActionsRefsInList(unittest.TestCase):
    def test_actions_refs_are_filtered_from_list(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            sha = _make_repo(root, files={"a.txt": b"hi\n"})
            output = _drive(backend, root / ".git", "push refs/heads/main:refs/heads/main\n\n")
            self.assertIn("ok refs/heads/main", output)
            asyncio.run(
                backend.cas_ref(
                    "refs/actions/repos/test/queue/x/y/z/run-1",
                    None,
                    ObjectId(sha),
                )
            )
            list_output = _drive(backend, root / ".git", "list\n\n")
        self.assertIn("refs/heads/main", list_output)
        self.assertNotIn("refs/actions/", list_output)


class FetchClosureTest(unittest.TestCase):
    def test_fetch_writes_objects_to_target_repo(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "src"
            sha = _make_repo(source, files={"a.txt": b"hello\n", "dir/b.txt": b"nested\n"})
            push_output = _drive(
                backend,
                source / ".git",
                "push refs/heads/main:refs/heads/main\n\n",
            )
            self.assertIn("ok refs/heads/main", push_output)
            target = tmp_path / "dst"
            target.mkdir()
            _git(target, "init", "--initial-branch=main", "-q")
            payload = f"fetch {sha} refs/heads/main\n\n"
            fetch_output = _drive(backend, target / ".git", payload)
            self.assertEqual(fetch_output, "\n")
            result = subprocess.run(
                [GIT, "-C", str(target), "cat-file", "-t", sha],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "commit")
            ls = subprocess.run(
                [GIT, "-C", str(target), "ls-tree", "-r", sha],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("a.txt", ls.stdout)
            self.assertIn("dir/b.txt", ls.stdout)


class FastForwardEnforcementTest(unittest.TestCase):
    def test_non_fast_forward_push_is_rejected(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_a = tmp_path / "a"
            sha_a = _make_repo(repo_a, files={"a.txt": b"a\n"})
            output_a = _drive(backend, repo_a / ".git", "push refs/heads/main:refs/heads/main\n\n")
            self.assertIn("ok refs/heads/main", output_a)
            self.assertEqual(asyncio.run(backend.read_ref("refs/heads/main")), ObjectId(sha_a))
            repo_b = tmp_path / "b"
            sha_b = _make_repo(repo_b, files={"b.txt": b"b\n"})
            output_b = _drive(backend, repo_b / ".git", "push refs/heads/main:refs/heads/main\n\n")
            self.assertIn("error refs/heads/main non-fast-forward", output_b)
            self.assertEqual(asyncio.run(backend.read_ref("refs/heads/main")), ObjectId(sha_a))
            self.assertNotEqual(sha_a, sha_b)


class ForcePushTest(unittest.TestCase):
    def test_force_push_overrides_non_fast_forward(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_a = tmp_path / "a"
            sha_a = _make_repo(repo_a, files={"a.txt": b"a\n"})
            _drive(backend, repo_a / ".git", "push refs/heads/main:refs/heads/main\n\n")
            self.assertEqual(asyncio.run(backend.read_ref("refs/heads/main")), ObjectId(sha_a))
            repo_b = tmp_path / "b"
            sha_b = _make_repo(repo_b, files={"b.txt": b"b\n"})
            output = _drive(backend, repo_b / ".git", "push +refs/heads/main:refs/heads/main\n\n")
        self.assertIn("ok refs/heads/main", output)
        self.assertEqual(asyncio.run(backend.read_ref("refs/heads/main")), ObjectId(sha_b))


class DeleteBranchTest(unittest.TestCase):
    def test_empty_src_deletes_dst_ref(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _make_repo(root, files={"a.txt": b"hi\n"})
            _drive(backend, root / ".git", "push refs/heads/main:refs/heads/main\n\n")
            self.assertIsNotNone(asyncio.run(backend.read_ref("refs/heads/main")))
            output = _drive(backend, root / ".git", "push :refs/heads/main\n\n")
        self.assertIn("ok refs/heads/main", output)
        self.assertIsNone(asyncio.run(backend.read_ref("refs/heads/main")))


class TagPushTest(unittest.TestCase):
    def test_annotated_tag_round_trips(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "src"
            _make_repo(source, files={"a.txt": b"hi\n"})
            _git(
                source,
                "-c",
                "user.email=tag@trunks.local",
                "-c",
                "user.name=Trunks Tag",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "tag.gpgsign=false",
                "tag",
                "-a",
                "v1.0",
                "-m",
                "release",
            )
            tag_sha = _git(source, "rev-parse", "v1.0").stdout.decode().strip()
            push_output = _drive(
                backend,
                source / ".git",
                "push refs/heads/main:refs/heads/main\npush refs/tags/v1.0:refs/tags/v1.0\n\n",
            )
            self.assertIn("ok refs/heads/main", push_output)
            self.assertIn("ok refs/tags/v1.0", push_output)
            target = tmp_path / "dst"
            target.mkdir()
            _git(target, "init", "--initial-branch=main", "-q")
            fetch_output = _drive(
                backend,
                target / ".git",
                f"fetch {tag_sha} refs/tags/v1.0\n\n",
            )
            self.assertEqual(fetch_output, "\n")
            tag_type = subprocess.run(
                [GIT, "-C", str(target), "cat-file", "-t", tag_sha],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(tag_type.stdout.strip(), "tag")


class OptionTest(unittest.TestCase):
    def test_known_option_returns_ok(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _make_repo(root, files={"a.txt": b"hi\n"})
            output = _drive(backend, root / ".git", "option progress true\n\n")
        self.assertIn("ok", output.split("\n"))

    def test_unknown_option_returns_unsupported(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _make_repo(root, files={"a.txt": b"hi\n"})
            output = _drive(backend, root / ".git", "option mystery xyz\n\n")
        self.assertIn("unsupported", output.split("\n"))

    def test_dry_run_skips_cas_update(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _make_repo(root, files={"a.txt": b"hi\n"})
            output = _drive(
                backend,
                root / ".git",
                "option dry-run true\npush refs/heads/main:refs/heads/main\n\n",
            )
        self.assertIn("ok refs/heads/main", output)
        self.assertIsNone(asyncio.run(backend.read_ref("refs/heads/main")))


class ReferencedShasTest(unittest.TestCase):
    def test_commit_refs_extracts_tree_and_parents(self) -> None:
        body = (
            b"tree 1234567890123456789012345678901234567890\n"
            b"parent abcdefabcdefabcdefabcdefabcdefabcdefabcd\n"
            b"author x <x@y> 0 +0000\n\n"
            b"msg\n"
        )
        raw = b"commit " + str(len(body)).encode() + b"\x00" + body
        refs = list(_referenced_shas(raw))
        self.assertEqual(
            [r.value for r in refs],
            [
                "1234567890123456789012345678901234567890",
                "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
            ],
        )

    def test_tree_refs_extracts_binary_entries(self) -> None:
        sha_bytes = bytes.fromhex("1234567890123456789012345678901234567890")
        body = b"100644 a.txt\x00" + sha_bytes
        raw = b"tree " + str(len(body)).encode() + b"\x00" + body
        refs = list(_referenced_shas(raw))
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].value, "1234567890123456789012345678901234567890")

    def test_blob_has_no_refs(self) -> None:
        body = b"hello"
        raw = b"blob " + str(len(body)).encode() + b"\x00" + body
        self.assertEqual(list(_referenced_shas(raw)), [])


if __name__ == "__main__":
    unittest.main()
