from __future__ import annotations

import asyncio
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

    delete_blob = Blob.from_data(b"delete-me")
    await backend.write_object(delete_blob.id, delete_blob.canonical())
    await backend.delete_object(delete_blob.id)
    testcase.assertFalse(await backend.has_object(delete_blob.id))

    contenders = [Blob.from_data(f"concurrent-{idx}".encode()) for idx in range(16)]
    for contender in contenders:
        await backend.write_object(contender.id, contender.canonical())
    winners = await asyncio.gather(
        *(backend.cas_ref("refs/heads/concurrent", None, contender.id) for contender in contenders)
    )
    testcase.assertEqual(sum(1 for item in winners if item), 1)
    testcase.assertIn(await backend.read_ref("refs/heads/concurrent"), {item.id for item in contenders})

    different_refs = await asyncio.gather(
        *(backend.cas_ref(f"refs/heads/concurrent-{idx}", None, contender.id) for idx, contender in enumerate(contenders))
    )
    testcase.assertEqual(different_refs, [True] * len(contenders))

    refs = []
    async for ref in backend.list_refs():
        refs.append((ref.name, ref.oid))
    testcase.assertIn(("refs/heads/contract", blob.id), refs)

    await backend.delete_ref("contract")
    testcase.assertIsNone(await backend.read_ref("contract"))

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
