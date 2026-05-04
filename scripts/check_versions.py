from __future__ import annotations

from sync_versions import main as sync_versions_main


def main() -> int:
    return sync_versions_main(["--check"])


if __name__ == "__main__":
    raise SystemExit(main())
