from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _python_version() -> str:
    raw = (ROOT / "trunks" / "version.py").read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', raw)
    if not match:
        raise SystemExit("trunks/version.py does not define __version__")
    return match.group(1)


def _toml_version(path: Path) -> str:
    project = tomllib.loads(path.read_text())["project"]
    if "version" in project:
        return project["version"]
    if "dynamic" in project and "version" in project["dynamic"]:
        return _python_version()
    raise SystemExit(f"{path.relative_to(ROOT)} has no project.version")


def _trunks_python_dependency() -> str:
    deps = tomllib.loads((ROOT / "trunks-python" / "pyproject.toml").read_text())["project"]["dependencies"]
    prefix = "trunks=="
    for dep in deps:
        if dep.startswith(prefix):
            return dep[len(prefix) :]
    raise SystemExit("trunks-python/pyproject.toml does not pin trunks==<version>")


def _node_version(path: Path) -> str:
    return json.loads(path.read_text())["version"]


def main() -> int:
    expected = _python_version()
    versions = {
        "pyproject.toml": _toml_version(ROOT / "pyproject.toml"),
        "trunks-python/pyproject.toml": _toml_version(ROOT / "trunks-python" / "pyproject.toml"),
        "trunks-python dependency": _trunks_python_dependency(),
        "trunks-node/package.json": _node_version(ROOT / "trunks-node" / "package.json"),
        "trunks-node/package-lock.json": json.loads((ROOT / "trunks-node" / "package-lock.json").read_text())["packages"][""]["version"],
    }
    mismatches = {name: version for name, version in versions.items() if version != expected}
    if mismatches:
        print(f"version mismatch: trunks/version.py={expected}", file=sys.stderr)
        for name, version in mismatches.items():
            print(f"  {name}={version}", file=sys.stderr)
        return 1
    print(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
