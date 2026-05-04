# Specs

A `Spec` describes the compute a job needs: CPU, memory, disk, architecture, GPU, network. The executor matches a job's `Spec` against what each provider says it can satisfy.

## Defaults

A job with no `runs-on` and no `trunks.spec` runs with the same shape as a GitHub-hosted `ubuntu-latest` runner: 2 vCPU, 7 GiB RAM, 14 GiB disk, host architecture, no GPU, default network.

## `runs-on` presets

Putting `runs-on:` on a job picks a preset.

| Label | CPU | Memory (GiB) | Disk (GiB) | GPU |
|---|---|---|---|---|
| `ubuntu-latest`, `ubuntu-24.04`, `ubuntu-22.04`, `ubuntu-20.04` | 2 | 7 | 14 | — |
| `ubuntu-large`, `ubuntu-latest-4-cores` | 4 | 16 | 150 | — |
| `ubuntu-latest-8-cores` | 8 | 32 | 300 | — |
| `ubuntu-latest-16-cores` | 16 | 64 | 600 | — |
| `ubuntu-latest-32-cores` | 32 | 128 | 1200 | — |
| `gpu-h100` | 8 | 32 | 100 | 1 × H100 80GB |
| `gpu-a100` | 8 | 32 | 100 | 1 × A100 80GB |
| `gpu-l40s` | 8 | 32 | 100 | 1 × L40S 48GB |

Unknown labels fall back to the default. List form is supported: the first known label wins.

## `trunks.spec` overrides

Any field in `trunks.spec` overrides the preset.

```yaml
jobs:
  train:
    runs-on: gpu-h100
    trunks:
      spec:
        cpu: 16
    steps:
      - run: nvidia-smi
```

The job runs on 16 CPU, 32 GiB RAM, 100 GiB disk, with one H100 — the preset's memory/disk/GPU stay; only CPU is overridden.

## Direct CLI

```bash
trunks actions run \
  --command "nvidia-smi" \
  --cpu 8 \
  --memory 32gb \
  --disk 100gb \
  --gpu h100 \
  --gpu-count 1 \
  --region us \
  --json
```

`--memory` and `--disk` accept human units. `--gpu` and `--gpu-count` are hard requirements; jobs requesting a GPU don't fall back to CPU.

## Matching

A provider's `info` declares the specs, regions, isolation modes, network modes, and GPU kinds it can satisfy. A provider that can't satisfy a `Spec` is skipped. With `--strict-provider`, a mismatched provider makes the run fail closed instead of routing elsewhere.

`spec.key` is the canonical hash of the resource shape. It is the indexing key for capacity and the queue. Two runs with different region preferences but identical shapes share the same key.

## Validation

`Spec` rejects invalid combinations at construction:

- CPU, memory, disk all `>= 1`
- `arch` is `x86_64` or `arm64`
- `network` is `default`, `none`, or `egress-only`
- `gpu.count >= 1` when `gpu` is set
