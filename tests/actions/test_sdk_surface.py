from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from trunks.actions.artifacts import get_artifact, list_artifacts, write_artifact
from trunks.actions.capacity import capacity_available, capacity_limit, capacity_snapshot, set_capacity_limit
from trunks.actions.run import enqueue_command, run_command
from trunks.actions.secrets import bind_secret, list_secret_bindings, remove_secret_binding, resolve_secret_env, show_secret_binding
from trunks.actions.storage import cancel_run, list_runs, load_run, prune_runs, repair_indexes
from trunks.actions.watch import watch_run_async
from trunks.actions.executor import execute_once
from trunks.actions.workflow import (
    lint_workflows,
    list_workflow_runs,
    load_workflow_run,
    migrate_workflow,
    parse_workflow,
    run_workflow,
    workflow_run_graph,
    workflow_run_jobs,
    workflow_run_logs,
)
from trunks.repository import Repository
from trunks.sandboxes import Spec
from trunks.sandboxes.contract import run_provider_contract
from trunks.sandboxes.registry import ProviderRegistry


class ActionsSdkSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_actions_sdk_functions_cover_full_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_secret = os.environ.get("TRUNKS_SDK_TOKEN")
            token_value = f"sdk-token-{root.name}"
            os.environ["TRUNKS_SDK_TOKEN"] = token_value
            try:
                repo = Repository.init(cwd=root, name="demo")
                cwd = Path.cwd()
                os.chdir(root)
                try:
                    run = await run_command("mkdir -p dist && printf sdk > dist/result.txt", cwd=str(root), artifact_paths=("dist",))
                    self.assertEqual(load_run(repo, run.id)["id"], run.id)
                    self.assertEqual(list_runs(repo, status="succeeded")[0]["id"], run.id)
                    watch_events = [event async for event in watch_run_async(repo, run.id, interval_s=0.01, timeout_s=1)]
                    self.assertEqual(watch_events[-1]["object"], "action_result")
                    self.assertEqual(list_artifacts(repo, run.id)[0]["name"], "dist/result.txt")
                    self.assertEqual(get_artifact(repo, run.id, "dist/result.txt"), b"sdk")
                    output = root / "out.txt"
                    write_artifact(repo, run.id, "dist/result.txt", output)
                    self.assertEqual(output.read_text(encoding="utf-8"), "sdk")

                    queued = enqueue_command(repo, "printf queued > queued.txt")
                    self.assertEqual(cancel_run(repo, queued.id)["state"]["phase"], "canceled")
                    executor_item = enqueue_command(repo, "printf executor > executor.txt")
                    executor_result = await execute_once(repo, executor="sdk-executor", cwd=str(root))
                    self.assertEqual(executor_result["id"], executor_item.id)
                    self.assertGreaterEqual(repair_indexes(repo)["repaired"], 2)

                    binding = bind_secret(repo, "TOKEN", "env:TRUNKS_SDK_TOKEN")
                    self.assertEqual(binding["name"], "TOKEN")
                    self.assertEqual(show_secret_binding(repo, "TOKEN")["source"], "env:TRUNKS_SDK_TOKEN")
                    self.assertEqual(list_secret_bindings(repo)[0]["name"], "TOKEN")
                    self.assertEqual(resolve_secret_env(repo)["TOKEN"], token_value)
                    self.assertTrue(remove_secret_binding(repo, "TOKEN")["deleted"])

                    spec_key = Spec(cpu=1, memory_gib=1, disk_gib=1, arch=_host_arch()).key
                    set_capacity_limit(repo, region="local", spec_key=spec_key, max_concurrent=3)
                    self.assertEqual(capacity_limit(repo, region="local", spec_key=spec_key), 3)
                    self.assertTrue(capacity_available(repo, region="local", spec_key=spec_key))
                    self.assertEqual(capacity_snapshot(repo)["limits"][0]["spec_key"], spec_key)
                    self.assertTrue(prune_runs(repo, keep_last=100, dry_run=True)["dry_run"])
                finally:
                    os.chdir(cwd)
            finally:
                if old_secret is None:
                    os.environ.pop("TRUNKS_SDK_TOKEN", None)
                else:
                    os.environ["TRUNKS_SDK_TOKEN"] = old_secret

    async def test_public_workflow_sdk_functions_cover_full_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            workflow_dir = root / ".trunks" / "workflows"
            workflow_dir.mkdir(parents=True)
            workflow_path = workflow_dir / "ci.yml"
            workflow_path.write_text(
                """
name: SDK CI
on: [push]
jobs:
  build:
    steps:
      - run: printf sdk-workflow && printf sdk-workflow > workflow.txt
""".strip(),
                encoding="utf-8",
            )
            workflow = parse_workflow(workflow_path, root=root)
            self.assertEqual(workflow.name, "SDK CI")
            self.assertTrue(lint_workflows(root)[0]["valid"])
            workflow_run = await run_workflow(repo, workflow="SDK CI", cwd=str(root))
            workflow_run_id = workflow_run["id"]
            self.assertEqual(load_workflow_run(repo, workflow_run_id)["id"], workflow_run_id)
            self.assertEqual(list_workflow_runs(repo)[0]["id"], workflow_run_id)
            self.assertEqual(workflow_run_jobs(repo, workflow_run_id)[0]["job"], "build")
            self.assertEqual(workflow_run_graph(repo, workflow_run_id), [])
            self.assertEqual(workflow_run_logs(repo, workflow_run_id)[0]["text"], "sdk-workflow")

            github = root / ".github" / "workflows"
            github.mkdir(parents=True)
            source = github / "ci.yml"
            source.write_text(workflow_path.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertTrue(migrate_workflow(source, dry_run=True)["dry_run"])


class SandboxesSdkSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_sandbox_sdk_functions_cover_registry_and_contract(self) -> None:
        registry = await ProviderRegistry.discover()
        provider = registry.get("local")
        resolved = await registry.resolve(
            capabilities=frozenset({next(iter(provider.info.capabilities - {"local"}))}),
            spec=provider.info.specs[0],
            region="local",
            isolation="process",
            network="default",
            provider="local",
            strict_provider=True,
        )
        self.assertEqual(resolved.info.id, "local")
        self.assertTrue((await run_provider_contract(resolved))["passed"])


def _host_arch() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


if __name__ == "__main__":
    unittest.main()
