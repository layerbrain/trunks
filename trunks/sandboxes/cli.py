from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict

from trunks.config import (
    remove_sandbox_provider_profile,
    set_sandbox_provider_profile,
)
from .contract import run_provider_contract
from .profile import SandboxProviderProfile, valid_sandbox_provider_name
from .registry import FIRST_PARTY_PROVIDERS, ProviderRegistry
from .scaffold import scaffold_provider


PROVIDER_ACTIONS = (
    "list",
    "ls",
    "show",
    "describe",
    "add",
    "rm",
    "delete",
    "doctor",
    "test",
    "benchmark",
    "scaffold",
    "orphans",
    "cleanup",
)


async def dispatch(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="trunks sandboxes")
    sub = parser.add_subparsers(dest="command")
    specs = sub.add_parser("specs")
    specs.add_argument("--provider", default=None)
    specs.add_argument("--json", action="store_true")
    regions = sub.add_parser("regions")
    regions.add_argument("--provider", default=None)
    regions.add_argument("--json", action="store_true")
    providers = sub.add_parser("providers")
    providers.add_argument("action", nargs="?", choices=PROVIDER_ACTIONS, default="list")
    providers.add_argument("id", nargs="?")
    providers.add_argument("--name", default=None)
    providers.add_argument("-o", "--output", default=None)
    providers.add_argument("--type", dest="provider_type", default=None)
    providers.add_argument("--priority", type=int, default=None)
    providers.add_argument("--enabled", dest="enabled", default=None, choices=["true", "false"])
    providers.add_argument("--api-key", dest="api_key", default=None)
    providers.add_argument("--region", dest="provider_region", default=None)
    providers.add_argument("--api-url", dest="api_url", default=None)
    providers.add_argument(
        "--set",
        dest="settings",
        action="append",
        default=[],
        metavar="key=value",
        help="set a non-secret setting (repeatable)",
    )
    providers.add_argument(
        "--secret",
        dest="secrets",
        action="append",
        default=[],
        metavar="key=value",
        help="set a credential value (repeatable, masked in output)",
    )
    providers.add_argument("--prefix", default="trunks-")
    providers.add_argument("--dry-run", action="store_true")
    providers.add_argument("--live", action="store_true")
    providers.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command in {None, "providers"}:
        return await _dispatch_providers(parser, args)
    registry = await _build_registry()
    if args.command == "specs":
        infos = [registry.get(args.provider).info] if args.provider else registry.list()
        data = [{"provider": info.id, "specs": [_spec_record(spec) for spec in info.specs]} for info in infos]
        print(json.dumps({"object": "list", "data": data, "total_count": len(data)}, sort_keys=True))
        return 0
    if args.command == "regions":
        infos = [registry.get(args.provider).info] if args.provider else registry.list()
        data = [{"provider": info.id, "regions": sorted(info.regions)} for info in infos]
        print(json.dumps({"object": "list", "data": data, "total_count": len(data)}, sort_keys=True))
        return 0
    return 0


async def _dispatch_providers(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.action == "ls":
        args.action = "list"
    if args.action == "describe":
        args.action = "show"
    if args.action == "delete":
        args.action = "rm"

    if args.action == "add":
        return _providers_add(parser, args)
    if args.action == "rm":
        return _providers_rm(parser, args)

    registry = await _build_registry()

    if args.action == "show":
        name = _provider_name(parser, args, action="show")
        registered = registry.get_registered(name)
        payload = _registered_record(registered)
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.action == "test":
        name = _provider_name(parser, args, action="test")
        provider = registry.get(name)
        _require_live_for_remote_provider(parser, provider, action="test", live=args.live)
        print(json.dumps(await run_provider_contract(provider), sort_keys=True))
        return 0
    if args.action == "doctor":
        name = _provider_name(parser, args, action="doctor")
        provider = registry.get(name)
        extra = await provider.doctor() if hasattr(provider, "doctor") else {}
        payload = {
            "_schema_version": "trunks.sandboxes.provider_doctor.v1",
            "object": "provider_doctor",
            "provider": name,
            "ok": True,
            "info": _provider_info(provider.info),
            "checks": extra,
        }
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.action == "benchmark":
        name = _provider_name(parser, args, action="benchmark")
        provider = registry.get(name)
        _require_live_for_remote_provider(parser, provider, action="benchmark", live=args.live)
        started = time.monotonic()
        result = await run_provider_contract(provider)
        payload = {
            "_schema_version": "trunks.sandboxes.provider_benchmark.v1",
            "object": "provider_benchmark",
            "provider": name,
            "contract": result,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.action == "scaffold":
        name = _provider_name(parser, args, action="scaffold")
        output = args.output or f"trunks-sandbox-{name}"
        path = scaffold_provider(name, output=output)
        payload = {
            "_schema_version": "trunks.sandboxes.provider_scaffold.v1",
            "object": "provider_scaffold",
            "path": str(path),
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        print(path)
        return 0
    if args.action in {"orphans", "cleanup"}:
        name = _provider_name(parser, args, action=args.action)
        provider = registry.get(name)
        if not hasattr(provider, "cleanup_orphans"):
            parser.error(f"provider {name!r} does not expose orphan cleanup")
        dry_run = args.dry_run or args.action == "orphans"
        payload = await provider.cleanup_orphans(prefix=args.prefix, dry_run=dry_run)  # type: ignore[attr-defined]
        print(json.dumps(payload, sort_keys=True))
        return 0
    data = [_registered_record(item) for item in registry.list_registered()]
    print(json.dumps({"object": "list", "data": data, "total_count": len(data)}, sort_keys=True))
    return 0


async def _build_registry() -> ProviderRegistry:
    return await ProviderRegistry.discover_async()


def _providers_add(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    name = _provider_name(parser, args, action="add")
    if not name:
        parser.error("providers add requires <name>")
    if not valid_sandbox_provider_name(name):
        parser.error(
            f"invalid provider name {name!r}: "
            "lowercase alphanumeric with hyphens or underscores"
        )
    if not args.provider_type:
        parser.error("providers add requires --type <type>")
    if not _known_provider_type(args.provider_type):
        parser.error(
            f"unknown provider type {args.provider_type!r}; "
            f"available: {', '.join(sorted(_all_provider_types()))}"
        )
    settings: dict[str, str] = {}
    credentials: dict[str, str] = {}
    if args.provider_region:
        settings["region"] = args.provider_region
    if args.api_url:
        settings["api_url"] = args.api_url
    if args.api_key:
        credentials["api_key"] = args.api_key
    for raw in args.settings:
        key, value = _parse_kv(parser, "--set", raw)
        settings[key] = value
    for raw in args.secrets:
        key, value = _parse_kv(parser, "--secret", raw)
        credentials[key] = value
    priority = args.priority if args.priority is not None else 100
    enabled = args.enabled != "false"
    profile = SandboxProviderProfile(
        name=name,
        type=args.provider_type,
        priority=priority,
        enabled=enabled,
        settings=settings,
        credentials=credentials,
    )
    set_sandbox_provider_profile(profile)
    payload = {
        "_schema_version": "trunks.sandboxes.provider_profile.v1",
        "object": "sandbox_provider_profile",
        **profile.masked_record(),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0
    print(f"Provider    {profile.name}  type={profile.type}  priority={profile.priority}")
    return 0


def _providers_rm(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    name = _provider_name(parser, args, action="rm")
    removed = remove_sandbox_provider_profile(name)
    if not removed:
        print(f"providers rm failed: unknown provider {name}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {"object": "sandbox_provider_profile", "name": name, "deleted": True},
                sort_keys=True,
            )
        )
        return 0
    print(f"Removed     {name}")
    return 0


def _provider_name(parser: argparse.ArgumentParser, args: argparse.Namespace, *, action: str) -> str:
    name = args.name or args.id
    if not name:
        parser.error(f"providers {action} requires --name <name>")
    return name


def _parse_kv(parser: argparse.ArgumentParser, flag: str, value: str) -> tuple[str, str]:
    if "=" not in value:
        parser.error(f"{flag} expects key=value, got {value!r}")
    key, _, val = value.partition("=")
    key = key.strip()
    if not key:
        parser.error(f"{flag} requires non-empty key")
    return key, val


def _known_provider_type(name: str) -> bool:
    return name in _all_provider_types()


def _all_provider_types() -> set[str]:
    return set(FIRST_PARTY_PROVIDERS.keys())


def _registered_record(item: object) -> dict[str, object]:
    info = getattr(item, "provider").info if hasattr(item, "provider") else item
    base = {
        "_schema_version": "trunks.sandboxes.provider_info.v1",
        "object": "sandbox_provider",
        "id": getattr(info, "id"),
        "capabilities": sorted(getattr(info, "capabilities")),
        "isolation": sorted(getattr(info, "isolation")),
        "architectures": sorted(getattr(info, "architectures")),
        "network_modes": sorted(getattr(info, "network_modes")),
        "gpu_kinds": sorted(getattr(info, "gpu_kinds")),
        "regions": sorted(getattr(info, "regions")),
        "max_cpu": getattr(info, "max_cpu"),
        "max_memory_gib": getattr(info, "max_memory_gib"),
        "max_disk_gib": getattr(info, "max_disk_gib"),
        "max_timeout_s": getattr(info, "max_timeout_s"),
        "specs": [_spec_record(spec) for spec in getattr(info, "specs")],
    }
    if hasattr(item, "name"):
        base["name"] = getattr(item, "name")
        base["type"] = getattr(item, "type")
        base["priority"] = getattr(item, "priority")
    return base


def _provider_info(info: object) -> dict[str, object]:
    return {
        "_schema_version": "trunks.sandboxes.provider_info.v1",
        "object": "sandbox_provider",
        "id": getattr(info, "id"),
        "capabilities": sorted(getattr(info, "capabilities")),
        "isolation": sorted(getattr(info, "isolation")),
        "architectures": sorted(getattr(info, "architectures")),
        "network_modes": sorted(getattr(info, "network_modes")),
        "gpu_kinds": sorted(getattr(info, "gpu_kinds")),
        "regions": sorted(getattr(info, "regions")),
        "max_cpu": getattr(info, "max_cpu"),
        "max_memory_gib": getattr(info, "max_memory_gib"),
        "max_disk_gib": getattr(info, "max_disk_gib"),
        "max_timeout_s": getattr(info, "max_timeout_s"),
        "specs": [_spec_record(spec) for spec in getattr(info, "specs")],
    }


def _spec_record(spec: object) -> dict[str, object]:
    payload = asdict(spec)
    payload["key"] = getattr(spec, "key")
    return payload


def _require_live_for_remote_provider(
    parser: argparse.ArgumentParser, provider: object, *, action: str, live: bool
) -> None:
    info = getattr(provider, "info", None)
    capabilities = getattr(info, "capabilities", frozenset())
    if "remote" in capabilities and not live:
        provider_id = getattr(info, "id", "<provider>")
        parser.error(
            f"providers {action} {provider_id} creates remote compute; pass --live to run it"
        )


def main() -> None:
    raise SystemExit(asyncio.run(dispatch(sys.argv[1:])))
