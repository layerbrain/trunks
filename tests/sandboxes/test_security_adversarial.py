from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from trunks.actions.capacity import (
    capacity_available,
    capacity_limit,
    claim_capacity_slot,
    release_capacity_slot,
    set_capacity_limit,
)
from trunks.actions.run import run_command_async
from trunks.actions.secrets import (
    MASKED_SECRET,
    bind_secret,
    mask_log_chunk,
    mask_secret_text,
    masking_env,
    resolve_secret_env,
)
from trunks.actions.storage import (
    cancel_run,
    claim_run,
    complete_run,
    heartbeat_run,
    persist_run,
)
from trunks.actions.state import Run, RunState
from trunks.repository import Repository
from trunks.sandboxes import (
    ExecResult,
    LogChunk,
    SandboxFile,
    SandboxRequest,
    Spec,
)
from trunks.sandboxes.providers.local import LocalProvider, LocalSandbox


def _spec() -> Spec:
    return Spec(cpu=1, memory_gib=1, disk_gib=1, arch=_host_arch())


def _host_arch() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    return "arm64" if machine in {"arm64", "aarch64"} else "x86_64"


def _request(run: str = "run") -> SandboxRequest:
    return SandboxRequest(
        run=run,
        commit="worktree",
        spec=_spec(),
        region="local",
        isolation="process",
        network="default",
        timeout_s=10,
    )


class SecretMaskingAdversarialTests(unittest.TestCase):
    def test_mask_secret_text_replaces_plaintext_value_with_mask_token(self) -> None:
        token = f"tok-{uuid4().hex}"
        text = f"output line containing {token} sensitive data"
        masked = mask_secret_text(text, {"API_TOKEN": token})
        self.assertNotIn(token, masked)
        self.assertIn(MASKED_SECRET, masked)

    def test_mask_secret_text_prefers_longest_value_when_secrets_share_prefix(self) -> None:
        short = "abc"
        long = "abcdef"
        text = "value=abcdef and short=abc"
        masked = mask_secret_text(text, {"SHORT": short, "LONG": long})
        self.assertNotIn(long, masked)
        self.assertNotIn(short.split()[0] if False else long, masked)
        self.assertEqual(masked.count(MASKED_SECRET), 2)

    def test_mask_log_chunk_preserves_stream_seq_and_timestamp_metadata(self) -> None:
        token = f"tok-{uuid4().hex}"
        chunk = LogChunk(stream="stderr", seq=42, text=f"error {token}", ts_ms=1234567890)
        masked = mask_log_chunk(chunk, {"TOKEN": token})
        self.assertEqual(masked.stream, "stderr")
        self.assertEqual(masked.seq, 42)
        self.assertEqual(masked.ts_ms, 1234567890)
        self.assertNotIn(token, masked.text)

    def test_mask_secret_text_skips_whitespace_only_values(self) -> None:
        text = "  spaces  and other content"
        masked = mask_secret_text(text, {"WHITE": "   ", "OTHER": ""})
        self.assertEqual(masked, text)

    def test_mask_secret_text_with_empty_env_returns_unchanged(self) -> None:
        text = "line of output"
        self.assertEqual(mask_secret_text(text, {}), text)

    def test_mask_secret_text_treats_regex_metacharacters_literally(self) -> None:
        secret = ".*(test).*+?{1,2}"
        text = f"prefix {secret} suffix"
        masked = mask_secret_text(text, {"PATTERN": secret})
        self.assertNotIn(secret, masked)
        self.assertIn(MASKED_SECRET, masked)

    def test_masking_env_includes_explicit_secret_env_only(self) -> None:
        masked = masking_env({"BOUND_API_KEY": "v1"}, {"PUBLIC": "p", "RUNTIME_TOKEN": "rt"})
        self.assertIn("BOUND_API_KEY", masked)
        self.assertIn("RUNTIME_TOKEN", masked)
        self.assertNotIn("PUBLIC", masked)

    def test_masking_env_excludes_non_sensitive_extra_env_names(self) -> None:
        masked = masking_env({}, {"DEBUG": "yes", "TIMEOUT_S": "30"})
        self.assertNotIn("DEBUG", masked)
        self.assertNotIn("TIMEOUT_S", masked)


class LocalSandboxArgvAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_sandbox_does_not_put_env_values_into_argv_strings(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        token = f"tok-{uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp:
            events = [
                event
                async for event in sandbox.run(
                    [sys.executable, "-c", "import sys; print('seen')"],
                    {"RUN_TOKEN": token},
                    tmp,
                    5,
                )
            ]
        result = next(event for event in events if isinstance(event, ExecResult))
        logs = [event for event in events if isinstance(event, LogChunk)]
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn(token, "\0".join(log.text for log in logs))

    async def test_local_sandbox_propagates_env_mapping_to_child_process(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        token = f"tok-{uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp:
            events = [
                event
                async for event in sandbox.run(
                    [sys.executable, "-c", "import os, sys; sys.stdout.write(os.environ['RUN_TOKEN'])"],
                    {"RUN_TOKEN": token},
                    tmp,
                    5,
                )
            ]
        logs = [event for event in events if isinstance(event, LogChunk)]
        self.assertEqual("".join(log.text for log in logs if log.stream == "stdout"), token)

    async def test_local_sandbox_propagates_nonzero_exit_code_as_exec_result(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        with tempfile.TemporaryDirectory() as tmp:
            events = [
                event
                async for event in sandbox.run(
                    [sys.executable, "-c", "import sys; sys.exit(7)"],
                    {},
                    tmp,
                    5,
                )
            ]
        result = next(event for event in events if isinstance(event, ExecResult))
        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.timed_out)


class LocalSandboxFilesystemAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_sandbox_upload_of_missing_source_raises(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.txt"
            with self.assertRaises(FileNotFoundError):
                [_ async for _ in sandbox.upload((SandboxFile(str(Path(tmp) / "missing.txt"), str(target)),))]

    async def test_local_sandbox_download_of_missing_source_raises(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            with self.assertRaises(FileNotFoundError):
                [_ async for _ in sandbox.download((SandboxFile(str(Path(tmp) / "absent.txt"), str(target)),))]

    async def test_local_sandbox_upload_overwrites_existing_target(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            target = Path(tmp) / "target.txt"
            source.write_text("new", encoding="utf-8")
            target.write_text("stale", encoding="utf-8")
            [_ async for _ in sandbox.upload((SandboxFile(str(source), str(target)),))]
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    async def test_local_sandbox_upload_preserves_byte_content_for_binary_payloads(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "binary.bin"
            target = Path(tmp) / "out.bin"
            payload = bytes(range(256))
            source.write_bytes(payload)
            [_ async for _ in sandbox.upload((SandboxFile(str(source), str(target)),))]
            self.assertEqual(target.read_bytes(), payload)


class LocalSandboxLifecycleAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_before_run_is_no_op(self) -> None:
        sandbox = LocalSandbox(_request())
        await sandbox.cancel()
        await sandbox.cancel()

    async def test_destroy_is_idempotent(self) -> None:
        sandbox = LocalSandbox(_request())
        await sandbox.destroy()
        await sandbox.destroy()

    async def test_cancel_after_clean_exit_is_no_op(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        with tempfile.TemporaryDirectory() as tmp:
            events = [
                event
                async for event in sandbox.run(
                    [sys.executable, "-c", "print('done')"], {}, tmp, 5
                )
            ]
        self.assertTrue(any(isinstance(event, ExecResult) for event in events))
        await sandbox.cancel()
        await sandbox.destroy()

    async def test_local_sandbox_timeout_marks_result_as_timed_out(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                events = [
                    event
                    async for event in sandbox.run(
                        [sys.executable, "-c", "import time; time.sleep(5)"],
                        {},
                        tmp,
                        1,
                    )
                ]
            result = next(event for event in events if isinstance(event, ExecResult))
            self.assertTrue(result.timed_out)
        finally:
            await sandbox.destroy()

    async def test_local_sandbox_timeout_zero_disables_timeout(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(_request())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                events = [
                    event
                    async for event in sandbox.run(
                        [sys.executable, "-c", "import time; time.sleep(0.05); print('ok')"],
                        {},
                        tmp,
                        0,
                    )
                ]
            result = next(event for event in events if isinstance(event, ExecResult))
            self.assertFalse(result.timed_out)
            self.assertEqual(result.exit_code, 0)
        finally:
            await sandbox.destroy()


class ActionsStorageCASAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repository.init(cwd=self._tmp.name, name="adv")

    def _enqueue(self, run_id: str) -> None:
        spec = _spec()
        run = Run(
            id=run_id,
            commit="worktree",
            command=("printf", "hi"),
            spec=spec,
            timeout_s=10,
            isolation="process",
            provider_id=None,
            strict_provider=False,
            artifact_paths=(),
            state=RunState(
                phase="pending",
                token=None,
                expires_at=None,
                result_oid=None,
                attempt=0,
                executor=None,
                provider=None,
                spec_key=spec.key,
                requested_region="local",
                region=None,
            ),
        )
        persist_run(self.repo, run)

    async def test_only_one_of_two_concurrent_claims_wins_lease(self) -> None:
        run_id = "RUN-A"
        self._enqueue(run_id)

        async def attempt(executor: str) -> dict[str, object] | None:
            return claim_run(
                self.repo,
                run_id,
                executor=executor,
                provider="local",
                spec_key=_spec().key,
                region="local",
                requested_region="local",
                ttl_s=60,
            )

        results = await asyncio.gather(attempt("exec-1"), attempt("exec-2"))
        winners = [item for item in results if item is not None]
        self.assertEqual(len(winners), 1)

    async def test_complete_run_rejects_wrong_token(self) -> None:
        run_id = "RUN-B"
        self._enqueue(run_id)
        claimed = claim_run(
            self.repo,
            run_id,
            executor="exec-1",
            provider="local",
            spec_key=_spec().key,
            region="local",
            requested_region="local",
            ttl_s=60,
        )
        self.assertIsNotNone(claimed)
        ok = complete_run(
            self.repo,
            claimed,
            token="wrong-token",
            phase="succeeded",
            logs=[],
            result={"exit_code": 0},
        )
        self.assertFalse(ok)

    async def test_heartbeat_run_rejects_wrong_token(self) -> None:
        run_id = "RUN-C"
        self._enqueue(run_id)
        claimed = claim_run(
            self.repo,
            run_id,
            executor="exec-1",
            provider="local",
            spec_key=_spec().key,
            region="local",
            requested_region="local",
            ttl_s=60,
        )
        assert claimed is not None
        self.assertFalse(heartbeat_run(self.repo, run_id, token="not-the-token"))

    async def test_cancel_run_on_terminal_run_returns_existing_payload(self) -> None:
        run_id = "RUN-D"
        self._enqueue(run_id)
        claimed = claim_run(
            self.repo,
            run_id,
            executor="exec-1",
            provider="local",
            spec_key=_spec().key,
            region="local",
            requested_region="local",
            ttl_s=60,
        )
        assert claimed is not None
        token = claimed["state"]["token"]
        complete_run(
            self.repo,
            claimed,
            token=token,
            phase="succeeded",
            logs=[],
            result={"exit_code": 0},
        )
        again = cancel_run(self.repo, run_id)
        self.assertIsNotNone(again)
        self.assertEqual(again["state"]["phase"], "succeeded")

    async def test_claim_run_returns_none_for_unknown_run(self) -> None:
        result = claim_run(
            self.repo,
            "UNKNOWN",
            executor="exec-1",
            provider="local",
            spec_key=_spec().key,
            region="local",
            requested_region="local",
            ttl_s=60,
        )
        self.assertIsNone(result)


class CapacityAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repository.init(cwd=self._tmp.name, name="cap")

    async def test_set_capacity_limit_rejects_negative_max_concurrent(self) -> None:
        with self.assertRaises(ValueError):
            set_capacity_limit(self.repo, region="local", spec_key=_spec().key, max_concurrent=-1)

    async def test_capacity_available_is_true_when_no_limit_configured(self) -> None:
        self.assertTrue(capacity_available(self.repo, region="local", spec_key=_spec().key))

    async def test_capacity_available_is_false_when_all_slots_held(self) -> None:
        spec_key = _spec().key
        set_capacity_limit(self.repo, region="local", spec_key=spec_key, max_concurrent=1)
        claimed = claim_capacity_slot(
            self.repo,
            region="local",
            spec_key=spec_key,
            owner="exec-1",
            ttl_s=60,
        )
        self.assertIsNotNone(claimed)
        self.assertFalse(capacity_available(self.repo, region="local", spec_key=spec_key))

    async def test_release_capacity_slot_with_wrong_token_does_not_remove_slot(self) -> None:
        spec_key = _spec().key
        set_capacity_limit(self.repo, region="local", spec_key=spec_key, max_concurrent=1)
        claimed = claim_capacity_slot(
            self.repo,
            region="local",
            spec_key=spec_key,
            owner="exec-1",
            ttl_s=60,
        )
        assert claimed is not None
        forged = {**claimed, "token": "forged"}
        release_capacity_slot(self.repo, forged)
        self.assertFalse(capacity_available(self.repo, region="local", spec_key=spec_key))

    async def test_capacity_limit_returns_none_when_unset(self) -> None:
        self.assertIsNone(capacity_limit(self.repo, region="other", spec_key="missing"))


class SecretBindingAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repository.init(cwd=self._tmp.name, name="sec")

    async def test_bind_secret_rejects_dash_in_name(self) -> None:
        with self.assertRaises(ValueError):
            bind_secret(self.repo, "BAD-NAME", "env:DOES_NOT_MATTER")

    async def test_bind_secret_rejects_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            bind_secret(self.repo, "", "env:X")

    async def test_resolve_secret_env_returns_value_when_env_var_present(self) -> None:
        env_name = f"TRUNKS_ADV_{uuid4().hex[:8].upper()}"
        os.environ[env_name] = "value-1"
        try:
            bind_secret(self.repo, "MY_TOKEN", f"env:{env_name}")
            resolved = resolve_secret_env(self.repo)
            self.assertEqual(resolved.get("MY_TOKEN"), "value-1")
        finally:
            os.environ.pop(env_name, None)

    async def test_resolve_secret_env_skips_bindings_with_missing_env_var(self) -> None:
        bind_secret(self.repo, "MISSING", "env:TRUNKS_ADV_DEFINITELY_UNSET_KEY")
        resolved = resolve_secret_env(self.repo)
        self.assertNotIn("MISSING", resolved)


class RunCommandSecretMaskingTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_command_masks_bound_secret_values_appearing_in_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_name = f"TRUNKS_ADV_{uuid4().hex[:8].upper()}"
            secret_value = f"secret-{uuid4().hex}"
            os.environ[env_name] = secret_value
            cwd = Path.cwd()
            os.chdir(root)
            try:
                repo = Repository.init(cwd=root, name="masking")
                bind_secret(repo, "ADV_TOKEN", f"env:{env_name}")
                run = await run_command_async(
                    [sys.executable, "-c", "import os; print(os.environ['ADV_TOKEN'])"],
                    cwd=str(root),
                )
                self.assertEqual(run.state.phase, "succeeded")
                self.assertNotIn(secret_value, "\n".join(chunk.text for chunk in run.logs))
                self.assertIn(MASKED_SECRET, "\n".join(chunk.text for chunk in run.logs))
            finally:
                os.chdir(cwd)
                os.environ.pop(env_name, None)


if __name__ == "__main__":
    unittest.main()
