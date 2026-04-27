from __future__ import annotations

import gc
import os
import threading
import time
import unittest
import warnings
from pathlib import Path

from trunks import Trunk


class TrunkLifecycleSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_thread_safety_concurrent_open_does_not_leak_engine(self) -> None:
        t = Trunk(backend="memory")
        engines: list[object] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def race() -> None:
            try:
                barrier.wait()
                engines.append(t.engine)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=race) for _ in range(8)]
        try:
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(engines), 8)
            self.assertEqual(len({id(e) for e in engines}), 1,
                             f"_ensure_open() raced: {len({id(e) for e in engines})} engines created")
        finally:
            t.close()

    def test_concurrent_sync_calls_are_serialized(self) -> None:
        t = Trunk(backend="memory")
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def write(index: int) -> None:
            try:
                barrier.wait()
                t.write(f"thread-{index}.txt", b"x")
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
        try:
            t.open()
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(t.repository.index_entries()), 8)
        finally:
            t.close()

    def test_sync_hot_loop_reuses_event_loop(self) -> None:
        t = Trunk(backend="memory")
        try:
            t.open()
            t.write("f1.txt", b"x")
            loop_after_first = t._sync_loop
            for i in range(50):
                t.write(f"f{i}.txt", b"x")
            self.assertIs(t._sync_loop, loop_after_first,
                          "sync loop was rebuilt mid-session")
            self.assertFalse(loop_after_first.is_closed(),
                             "sync loop was closed mid-session")
        finally:
            t.close()

    def test_orphaned_memory_trunk_does_not_leak_tempdir(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            t = Trunk(backend="memory")
            t.open()
            tmpdir = Path(t._tmpdir.name)
            self.assertTrue(tmpdir.exists())
            del t
            gc.collect()
        self.assertFalse(tmpdir.exists(),
                         f"orphaned memory Trunk leaked tempdir: {tmpdir}")
        self.assertEqual(
            [warning for warning in caught if issubclass(warning.category, ResourceWarning)],
            [],
        )

    def test_close_from_running_loop_after_sync_open_closes_persistent_loop(self) -> None:
        t = Trunk(backend="memory")
        t.open()
        loop = t._sync_loop
        self.assertIsNotNone(loop)

        async def close_in_loop() -> None:
            result = t.close()
            if result is not None:
                await result

        import asyncio
        asyncio.run(close_in_loop())

        self.assertTrue(loop.is_closed())
        self.assertIsNone(t._sync_loop)

    def test_reuse_after_close_is_refused(self) -> None:
        t = Trunk(backend="memory")
        t.open()
        t.write("a.txt", b"x")
        t.close()
        with self.assertRaises(RuntimeError, msg="reused closed Trunk silently re-opened"):
            t.write("b.txt", b"y")

    def test_two_memory_trunks_do_not_share_state(self) -> None:
        a = Trunk(backend="memory")
        b = Trunk(backend="memory")
        try:
            a.open()
            b.open()
            a.write("a.txt", b"only-in-a")
            with self.assertRaises(Exception):
                b.read("a.txt")
        finally:
            a.close()
            b.close()

    def test_misspelled_scheme_is_not_silently_a_local_db(self) -> None:
        bad = "s3:/typo-only-one-slash/bucket"
        with self.assertRaises(Exception, msg=f"{bad!r} silently treated as local DB"):
            t = Trunk(backend=bad)
            try:
                t.open()
            finally:
                t.close()
        self.assertFalse(Path(bad).exists(),
                         f"misspelled URL created path on disk: {bad}")


if __name__ == "__main__":
    unittest.main()
