from __future__ import annotations

import tempfile
import unittest

from trunks.backend import Backend
from trunks.engine import Engine
from trunks.ids import ObjectId
from trunks.objects import Blob
from trunks.repository import Repository


async def assert_backend_contract(testcase: unittest.TestCase, backend: Backend) -> None:
    capabilities = await backend.capabilities()
    testcase.assertTrue(capabilities.journal)
    testcase.assertTrue(capabilities.list_prefix)

    blob = Blob.from_data(b"contract-object")
    await backend.write_object(blob.id, blob.canonical())
    testcase.assertEqual(await backend.read_object(blob.id), blob.canonical())

    batch = [Blob.from_data(f"batch-{idx}".encode()) for idx in range(4)]
    await backend.write_objects((item.id, item.canonical()) for item in batch)
    for item in batch:
        testcase.assertEqual(await backend.read_object(item.id), item.canonical())

    config = {"format": 1, "repository": "contract", "storage": [{"name": "primary", "role": "primary", "url": "memory://"}]}
    await backend.write_config(config)
    testcase.assertEqual(await backend.read_config(), config)

    testcase.assertTrue(await backend.cas_ref("contract", None, blob.id))
    testcase.assertEqual(await backend.read_ref("contract"), blob.id)
    testcase.assertFalse(await backend.cas_ref("contract", None, ObjectId("1" * 40)))

    refs = []
    async for ref in backend.list_refs():
        refs.append((ref.name, ref.oid))
    testcase.assertIn(("refs/heads/contract", blob.id), refs)

    with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as dest_dir:
        source = Repository.init(source_dir)
        source_engine = Engine(source, backend)
        await source_engine.write("README.md", b"# contract\n")
        commit = await source_engine.commit(message="init")
        await source_engine.push()

        dest = Repository.init(dest_dir)
        dest_engine = Engine(dest, backend)
        await dest_engine.pull()

        testcase.assertEqual(dest.ref("main"), commit.id)
        testcase.assertEqual((dest.root / "README.md").read_bytes(), b"# contract\n")
