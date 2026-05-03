from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from trunks.cli import dispatch
from trunks.repository import Repository
from trunks.sandboxes import Spec


class ActionsCliSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_actions_cli_command_and_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                Repository.init(name="demo")
                (root / ".trunks" / "workflows").mkdir(parents=True)
                (root / ".trunks" / "workflows" / "ci.yml").write_text(
                    """
name: CI
on: push
jobs:
  build:
    steps:
      - run: printf workflow && printf workflow > workflow.txt
      - run: mkdir -p reports && printf report > reports/result.txt
      - uses: actions/upload-artifact@v4
        with:
          path: reports/result.txt
""".strip(),
                    encoding="utf-8",
                )

                run = await _json_cli(["actions", "run", "--command", "printf artifact && mkdir -p dist && printf artifact > dist/a.txt", "--artifact", "dist", "--json"])
                run_id = run["id"]
                self.assertEqual(run["state"]["phase"], "succeeded")

                listed = await _json_cli(["actions", "list", "--json"])
                listed_alias = await _json_cli(["actions", "ls", "--json"])
                self.assertEqual(listed["data"][0]["id"], run_id)
                self.assertEqual(listed_alias["data"][0]["id"], run_id)
                self.assertEqual((await _json_cli(["actions", "status", "--id", run_id, "--json"]))["phase"], "succeeded")
                self.assertEqual((await _json_cli(["actions", "describe", "--id", run_id, "--json"]))["id"], run_id)
                self.assertEqual((await _json_cli(["actions", "get", "--id", run_id, "--json"]))["id"], run_id)
                self.assertEqual((await _json_cli(["actions", "logs", "--id", run_id, "--json"]))["data"][0]["text"], "artifact")
                watch_events = await _json_lines_cli(["actions", "watch", "--id", run_id, "--json"])
                self.assertEqual(watch_events[-1]["object"], "action_result")
                self.assertEqual((await _json_cli(["actions", "artifacts", "--id", run_id, "--json"]))["data"][0]["name"], "dist/a.txt")
                target = root / "download.txt"
                self.assertEqual(await _cli(["actions", "artifacts", "--id", run_id, "get", "dist/a.txt", "-o", str(target)]), "")
                self.assertEqual(target.read_text(encoding="utf-8"), "artifact")

                queued = await _json_cli(["actions", "enqueue", "--command", "printf queued > queued.txt", "--json"])
                canceled = await _json_cli(["actions", "cancel", "--id", queued["id"], "--json"])
                self.assertEqual(canceled["state"]["phase"], "canceled")
                executor_item = await _json_cli(["actions", "enqueue", "--command", "printf executor > executor.txt", "--json"])
                executor = await _json_cli(["actions", "execute", "--id", "cli-executor", "--json"])
                self.assertEqual(executor["run"]["id"], executor_item["id"])
                skipped = await _json_cli(["actions", "enqueue", "--command", "printf skipped > skipped.txt", "--json"])
                pinned = await _json_cli(["actions", "enqueue", "--command", "printf pinned > pinned.txt", "--json"])
                pinned_result = await _json_cli(["actions", "execute", "--id", "cli-executor", "--run-id", pinned["id"], "--json"])
                self.assertEqual(pinned_result["run"]["id"], pinned["id"])
                self.assertFalse((root / "skipped.txt").exists())

                secret = await _json_cli(["actions", "secrets", "bind", "TOKEN", "env:TRUNKS_SURFACE_TOKEN", "--json"])
                self.assertEqual(secret["source_type"], "env")
                self.assertNotIn("source", secret)
                self.assertEqual((await _json_cli(["actions", "secrets", "show", "TOKEN", "--json"]))["name"], "TOKEN")
                self.assertEqual((await _json_cli(["actions", "secrets", "ls", "--json"]))["data"][0]["name"], "TOKEN")
                self.assertTrue((await _json_cli(["actions", "secrets", "remove", "TOKEN", "--json"]))["deleted"])
                await _json_cli(["actions", "secrets", "bind", "TOKEN2", "env:TRUNKS_SURFACE_TOKEN", "--json"])
                self.assertTrue((await _json_cli(["actions", "secrets", "rm", "TOKEN2", "--json"]))["deleted"])

                spec_key = Spec(cpu=1, memory_gib=1, disk_gib=1, arch=_host_arch()).key
                self.assertEqual((await _json_cli(["actions", "capacity", "set", "local", spec_key, "max-concurrent", "2", "--json"]))["max_concurrent"], 2)
                self.assertEqual((await _json_cli(["actions", "capacity", "show", "--json"]))["limits"][0]["max_concurrent"], 2)
                self.assertTrue((await _json_cli(["actions", "storage", "doctor", "--json"]))["ok"])
                self.assertGreaterEqual((await _json_cli(["actions", "index", "repair", "--json"]))["repaired"], 1)

                workflows = await _json_cli(["actions", "workflows", "list", "--json"])
                self.assertEqual(workflows["data"][0]["name"], "CI")
                self.assertTrue((await _json_cli(["actions", "workflows", "lint", "--json"]))["data"][0]["valid"])
                self.assertEqual((await _json_cli(["actions", "workflows", "show", "CI", "--json"]))["name"], "CI")
                workflow_run = await _json_cli(["actions", "run", "CI", "--json"])
                workflow_run_id = workflow_run["id"]
                self.assertEqual(workflow_run["phase"], "succeeded")
                self.assertEqual((await _json_cli(["actions", "workflow-runs", "--json"]))["data"][0]["id"], workflow_run_id)
                self.assertEqual((await _json_cli(["actions", "workflow-runs", "--id", workflow_run_id, "--json"]))["id"], workflow_run_id)
                self.assertEqual((await _json_cli(["actions", "workflow-runs", "--id", workflow_run_id, "jobs", "--json"]))["data"][0]["job"], "build")
                self.assertEqual((await _json_cli(["actions", "workflow-runs", "--id", workflow_run_id, "graph", "--json"]))["data"], [])
                self.assertEqual((await _json_cli(["actions", "workflow-runs", "--id", workflow_run_id, "logs", "--json"]))["data"][0]["job"], "build")

                github = root / ".github" / "workflows"
                github.mkdir(parents=True)
                source = github / "import.yml"
                source.write_text("name: Imported\non: push\njobs:\n  test:\n    steps:\n      - run: echo imported\n", encoding="utf-8")
                migration = await _json_cli(["actions", "migrate", str(source), "--dry-run", "--json"])
                self.assertTrue(migration["dry_run"])
                prune = await _json_cli(["actions", "prune", "--keep-last", "100", "--dry-run", "--json"])
                self.assertTrue(prune["dry_run"])
            finally:
                os.chdir(cwd)


class SandboxesCliSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_sandboxes_cli_command(self) -> None:
        listed = await _json_cli(["sandboxes", "providers", "--json"])
        provider_ids = {item["id"] for item in listed["data"]}
        self.assertIn("local", provider_ids)
        self.assertEqual((await _json_cli(["sandboxes", "providers", "show", "local"]))["id"], "local")
        self.assertTrue((await _json_cli(["sandboxes", "providers", "doctor", "local", "--json"]))["ok"])
        self.assertTrue((await _json_cli(["sandboxes", "providers", "test", "local", "--json"]))["passed"])
        self.assertTrue((await _json_cli(["sandboxes", "providers", "benchmark", "local", "--json"]))["contract"]["passed"])
        local_specs = await _json_cli(["sandboxes", "specs", "--provider", "local", "--json"])
        self.assertEqual(local_specs["data"][0]["provider"], "local")
        self.assertEqual(local_specs["data"][0]["specs"][0]["cpu"], 1)
        self.assertIn("key", local_specs["data"][0]["specs"][0])
        self.assertEqual((await _json_cli(["sandboxes", "regions", "--provider", "local", "--json"]))["data"][0]["regions"], ["local"])
        with tempfile.TemporaryDirectory() as tmp:
            scaffold = await _json_cli(["sandboxes", "providers", "scaffold", "edge", "-o", tmp, "--json"])
            self.assertTrue(Path(scaffold["path"]).exists())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                Repository.init(name="providers")
                added = await _json_cli(
                    [
                        "sandboxes",
                        "providers",
                        "add",
                        "do-main",
                        "--type",
                        "digitalocean",
                        "--secret",
                        "api_key=test-token",
                        "--set",
                        "region=nyc3",
                        "--json",
                    ]
                )
                self.assertEqual(added["credentials"]["api_key"], "****")
                self.assertEqual(added["settings"]["region"], "nyc3")
                shown = await _json_cli(["sandboxes", "providers", "show", "do-main", "--json"])
                self.assertEqual(shown["name"], "do-main")
                self.assertEqual(shown["type"], "digitalocean")
                self.assertEqual(shown["specs"][0]["cpu"], 1)
                self.assertIn("key", shown["specs"][0])
                removed = await _json_cli(["sandboxes", "providers", "rm", "do-main", "--json"])
                self.assertTrue(removed["deleted"])
            finally:
                os.chdir(cwd)


async def _json_cli(args: list[str]) -> dict[str, object]:
    output = await _cli(args)
    return json.loads(output)


async def _json_lines_cli(args: list[str]) -> list[dict[str, object]]:
    output = await _cli(args)
    return [json.loads(line) for line in output.splitlines() if line.strip()]


async def _cli(args: list[str]) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        code = await dispatch(args)
    if code != 0:
        raise AssertionError(f"{args} exited {code}")
    return out.getvalue()


def _host_arch() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


if __name__ == "__main__":
    unittest.main()
