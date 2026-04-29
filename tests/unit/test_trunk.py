from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path

from trunks import Trunk
from trunks.backends.memory import Memory


class EnterRequiredMemory(Memory):
    def __init__(self) -> None:
        super().__init__()
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def write_object(self, oid, data) -> None:
        if not self.entered:
            raise AssertionError("backend was not entered before write_object")
        await super().write_object(oid, data)

    async def read_ref(self, name):
        if not self.entered:
            raise AssertionError("backend was not entered before read_ref")
        return await super().read_ref(name)

    async def cas_ref(self, name, expected, new):
        if not self.entered:
            raise AssertionError("backend was not entered before cas_ref")
        return await super().cas_ref(name, expected, new)


class TrunkTests(unittest.IsolatedAsyncioTestCase):
    def test_sync_public_api(self) -> None:
        with Trunk(backend="memory") as trunk:
            trunk.write("README.md", b"# hi\n")
            trunk.write("src/app.py", b"print('hi')\n")
            commit = trunk.commit(message="init")
            self.assertEqual(trunk.read("README.md"), b"# hi\n")
            self.assertEqual(trunk.list(), ["README.md", "src"])
            self.assertEqual(trunk.list("src"), ["app.py"])
            self.assertTrue(trunk.exists("src/app.py"))
            self.assertTrue(trunk.exists("src"))
            trunk.copy("src/app.py", "src/copy.py")
            self.assertEqual(trunk.read("src/copy.py"), b"print('hi')\n")
            trunk.move("src/copy.py", "src/moved.py")
            self.assertFalse(trunk.exists("src/copy.py"))
            self.assertEqual(trunk.read("src/moved.py"), b"print('hi')\n")
            trunk.mkdir("generated")
            trunk.remove("src/app.py")
            self.assertFalse(trunk.exists("src/app.py"))
            self.assertEqual(trunk.status().head, commit.id)

    async def test_async_public_api(self) -> None:
        async with Trunk(backend="memory") as trunk:
            await trunk.write("README.md", b"# hi\n")
            await trunk.write("src/app.py", b"print('hi')\n")
            commit = await trunk.commit(message="init")
            self.assertEqual(await trunk.read("README.md"), b"# hi\n")
            self.assertEqual(await trunk.list(), ["README.md", "src"])
            self.assertEqual(await trunk.list("src"), ["app.py"])
            self.assertTrue(await trunk.exists("src/app.py"))
            await trunk.copy("src/app.py", "src/copy.py")
            self.assertEqual(await trunk.read("src/copy.py"), b"print('hi')\n")
            await trunk.move("src/copy.py", "src/moved.py")
            self.assertFalse(await trunk.exists("src/copy.py"))
            await trunk.mkdir("generated")
            await trunk.remove("src/app.py")
            self.assertFalse(await trunk.exists("src/app.py"))
            status = await trunk.status()
            self.assertEqual(status.head, commit.id)

    def test_open_close_sync_lifecycle(self) -> None:
        t = Trunk(backend="memory")
        try:
            self.assertIs(t.open(), t)
            t.write("a.txt", b"hi")
            self.assertEqual(t.read("a.txt"), b"hi")
        finally:
            t.close()

    async def test_open_close_async_lifecycle(self) -> None:
        t = Trunk(backend="memory")
        try:
            self.assertIs(await t.open(), t)
            await t.write("a.txt", b"hi")
            self.assertEqual(await t.read("a.txt"), b"hi")
        finally:
            await t.close()

    async def test_sync_with_inside_running_loop_is_refused(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            with Trunk(backend="memory"):
                pass
        self.assertIn("running asyncio loop", str(cm.exception))

    async def test_push_enters_backend_before_io(self) -> None:
        backend = EnterRequiredMemory()
        with tempfile.TemporaryDirectory() as workdir:
            previous = os.getcwd()
            os.chdir(workdir)
            try:
                async with Trunk(backend=backend) as trunk:
                    await trunk.write("README.md", b"# hi\n")
                    await trunk.commit(message="init")
                    await trunk.push()
            finally:
                os.chdir(previous)
        self.assertTrue(backend.entered)

    def test_backend_base_and_name_resolve_to_trunk_path(self) -> None:
        with tempfile.TemporaryDirectory() as backend_root, tempfile.TemporaryDirectory() as workdir:
            previous = os.getcwd()
            os.chdir(workdir)
            try:
                backend = f"local://{backend_root}"
                with Trunk(backend=backend, name="lazy-lms") as trunk:
                    self.assertEqual(trunk.repository.name, "lazy-lms")
                    self.assertEqual(trunk.repository.backend_url(), f"{backend}/trunks/lazy-lms.trunk")
                    trunk.write("README.md", b"# hi\n")
                    trunk.commit(message="init")
                    trunk.push()
                self.assertTrue((Path(backend_root) / "trunks" / "lazy-lms.trunk" / "refs" / "heads" / "main").exists())
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
