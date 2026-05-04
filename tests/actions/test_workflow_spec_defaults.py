from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from trunks.actions.run import DEFAULT_CPU, DEFAULT_DISK_GIB, DEFAULT_MEMORY_GIB
from trunks.actions.workflow import parse_workflow


def _host_arch() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


def _write_workflow(root: Path, body: str) -> Path:
    workflow_dir = root / ".trunks" / "workflows"
    workflow_dir.mkdir(parents=True)
    path = workflow_dir / "ci.yml"
    path.write_text(body, encoding="utf-8")
    return path


def _spec(body: str):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_workflow(root, body)
        workflow = parse_workflow(path, root=root)
        return workflow.jobs[0].spec


class WorkflowSpecDefaultsTests(unittest.TestCase):
    def test_empty_job_uses_github_hosted_default(self) -> None:
        spec = _spec(
            """
name: CI
on: [push]
jobs:
  build:
    steps:
      - run: echo hi
""".strip()
        )
        self.assertEqual(spec.cpu, DEFAULT_CPU)
        self.assertEqual(spec.memory_gib, DEFAULT_MEMORY_GIB)
        self.assertEqual(spec.disk_gib, DEFAULT_DISK_GIB)
        self.assertEqual(spec.arch, _host_arch())
        self.assertIsNone(spec.gpu)
        self.assertEqual(spec.network, "default")

    def test_runs_on_ubuntu_latest_matches_github_hosted(self) -> None:
        spec = _spec(
            """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
""".strip()
        )
        self.assertEqual((spec.cpu, spec.memory_gib, spec.disk_gib), (2, 7, 14))

    def test_runs_on_ubuntu_large_matches_4_core_runner(self) -> None:
        spec = _spec(
            """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-large
    steps:
      - run: echo hi
""".strip()
        )
        self.assertEqual((spec.cpu, spec.memory_gib, spec.disk_gib), (4, 16, 150))

    def test_runs_on_8_core_runner_matches_github_pricing_table(self) -> None:
        spec = _spec(
            """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest-8-cores
    steps:
      - run: echo hi
""".strip()
        )
        self.assertEqual((spec.cpu, spec.memory_gib, spec.disk_gib), (8, 32, 300))

    def test_runs_on_gpu_h100_attaches_gpu_descriptor(self) -> None:
        spec = _spec(
            """
name: CI
on: [push]
jobs:
  train:
    runs-on: gpu-h100
    steps:
      - run: nvidia-smi
""".strip()
        )
        self.assertEqual((spec.cpu, spec.memory_gib, spec.disk_gib), (8, 32, 100))
        self.assertIsNotNone(spec.gpu)
        self.assertEqual(spec.gpu.kind, "H100_80GB")
        self.assertEqual(spec.gpu.count, 1)

    def test_runs_on_list_form_resolves_first_known_label(self) -> None:
        spec = _spec(
            """
name: CI
on: [push]
jobs:
  build:
    runs-on: [ubuntu-latest]
    steps:
      - run: echo hi
""".strip()
        )
        self.assertEqual((spec.cpu, spec.memory_gib, spec.disk_gib), (2, 7, 14))

    def test_unknown_runs_on_label_falls_back_to_default(self) -> None:
        spec = _spec(
            """
name: CI
on: [push]
jobs:
  build:
    runs-on: self-hosted-private
    steps:
      - run: echo hi
""".strip()
        )
        self.assertEqual(
            (spec.cpu, spec.memory_gib, spec.disk_gib),
            (DEFAULT_CPU, DEFAULT_MEMORY_GIB, DEFAULT_DISK_GIB),
        )

    def test_trunks_spec_overrides_runs_on_preset_per_field(self) -> None:
        spec = _spec(
            """
name: CI
on: [push]
jobs:
  train:
    runs-on: gpu-h100
    trunks:
      spec:
        cpu: 16
    steps:
      - run: nvidia-smi
""".strip()
        )
        self.assertEqual(spec.cpu, 16)
        self.assertEqual(spec.memory_gib, 32)
        self.assertEqual(spec.disk_gib, 100)
        self.assertEqual(spec.gpu.kind, "H100_80GB")

    def test_trunks_spec_gpu_alone_keeps_default_resources(self) -> None:
        spec = _spec(
            """
name: CI
on: [push]
jobs:
  train:
    trunks:
      spec:
        gpu:
          kind: H200_141GB
          count: 1
    steps:
      - run: nvidia-smi
""".strip()
        )
        self.assertEqual(spec.cpu, DEFAULT_CPU)
        self.assertEqual(spec.memory_gib, DEFAULT_MEMORY_GIB)
        self.assertEqual(spec.disk_gib, DEFAULT_DISK_GIB)
        self.assertEqual(spec.gpu.kind, "H200_141GB")


if __name__ == "__main__":
    unittest.main()
