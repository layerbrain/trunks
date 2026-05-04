#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3.12}"

"$python_bin" -m compileall -q trunks tests/actions tests/sandboxes
"$python_bin" -m unittest discover -s tests/actions
"$python_bin" -m unittest discover -s tests/sandboxes
