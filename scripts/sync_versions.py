from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATH = ROOT / "trunks" / "version.py"


def root_version() -> str:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', VERSION_PATH.read_text())
    if not match:
        raise SystemExit("trunks/version.py does not define __version__")
    return match.group(1)


def set_root_version(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9._-]+)?", version):
        raise SystemExit(f"invalid version: {version!r}")
    _replace_text(VERSION_PATH, r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"')


def desired_files(version: str) -> dict[Path, str]:
    trunks_python = ROOT / "trunks-python" / "pyproject.toml"
    package_json = ROOT / "trunks-node" / "package.json"
    package_lock = ROOT / "trunks-node" / "package-lock.json"

    trunks_python_text = trunks_python.read_text()
    trunks_python_text = re.sub(r'^version = "[^"]+"', f'version = "{version}"', trunks_python_text, count=1, flags=re.MULTILINE)
    trunks_python_text = re.sub(r'trunks==[^"]+', f"trunks=={version}", trunks_python_text, count=1)

    package_data = json.loads(package_json.read_text())
    package_data["version"] = version

    lock_data = json.loads(package_lock.read_text())
    lock_data["version"] = version
    lock_data["packages"][""]["version"] = version

    return {
        trunks_python: trunks_python_text,
        package_json: json.dumps(package_data, indent=2) + "\n",
        package_lock: json.dumps(lock_data, indent=2) + "\n",
    }


def sync(version: str) -> None:
    for path, text in desired_files(version).items():
        path.write_text(text)


def check(version: str) -> bool:
    ok = True
    for path, expected in desired_files(version).items():
        if path.read_text() != expected:
            print(f"{path.relative_to(ROOT)} is not synced to trunks/version.py={version}", file=sys.stderr)
            ok = False
    return ok


def _replace_text(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text()
    updated, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        raise SystemExit(f"could not update {path.relative_to(ROOT)}")
    path.write_text(updated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync package manifest versions from trunks/version.py.")
    parser.add_argument("--set", dest="new_version", default=None, help="update trunks/version.py before syncing")
    parser.add_argument("--check", action="store_true", help="only check generated manifests")
    args = parser.parse_args(argv)

    if args.new_version:
        if args.check:
            parser.error("--set and --check cannot be used together")
        set_root_version(args.new_version)

    version = root_version()
    if args.check:
        if check(version):
            print(version)
            return 0
        print("run: python scripts/sync_versions.py", file=sys.stderr)
        return 1

    sync(version)
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
