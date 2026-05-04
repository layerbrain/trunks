from __future__ import annotations

import importlib
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

from trunks._auto import run_auto
from trunks.config import load_sandbox_provider_profiles
from .profile import SandboxProviderProfile
from .provider import NetworkMode, SandboxProvider, SandboxProviderInfo, Spec, Isolation


FIRST_PARTY_PROVIDERS: dict[str, str] = {
    "local": "trunks.sandboxes.providers.local:LocalProvider",
    "docker": "trunks.sandboxes.providers.docker:DockerProvider",
    "daytona": "trunks.sandboxes.providers.daytona:DaytonaProvider",
    "digitalocean": "trunks.sandboxes.providers.digitalocean:DigitalOceanProvider",
    "primeintellect": "trunks.sandboxes.providers.primeintellect:PrimeIntellectProvider",
}


@dataclass(frozen=True)
class RegisteredProvider:
    name: str
    type: str
    priority: int
    provider: SandboxProvider


class ProviderRegistry:
    def __init__(
        self,
        registered: list[RegisteredProvider] | dict[str, SandboxProvider],
    ) -> None:
        if isinstance(registered, dict):
            registered = [
                RegisteredProvider(name=key, type=key, priority=100 + index, provider=value)
                for index, (key, value) in enumerate(registered.items())
            ]
        self._registered = sorted(registered, key=lambda item: (item.priority, item.name))
        self._by_name: dict[str, RegisteredProvider] = {item.name: item for item in self._registered}
        self._by_type: dict[str, RegisteredProvider] = {}
        for item in self._registered:
            self._by_type.setdefault(item.type, item)

    @classmethod
    def discover(
        cls,
        config: dict[str, object] | None = None,
        *,
        profiles: Iterable[SandboxProviderProfile] | None = None,
    ) -> "ProviderRegistry" | Awaitable["ProviderRegistry"]:
        return run_auto(lambda: cls.discover_async(config, profiles=profiles))

    @classmethod
    async def discover_async(
        cls,
        config: dict[str, object] | None = None,
        *,
        profiles: Iterable[SandboxProviderProfile] | None = None,
    ) -> "ProviderRegistry":
        raw_config = config or {}
        registered: list[RegisteredProvider] = []
        for index, (provider_id, path) in enumerate(FIRST_PARTY_PROVIDERS.items()):
            provider_config = _provider_config(raw_config, provider_id)
            if provider_config.get("enabled", True) is False:
                continue
            provider_cls = _load_provider(path)
            try:
                instance = await provider_cls.discover(provider_config)
            except LookupError:
                continue
            registered.append(
                RegisteredProvider(
                    name=provider_id,
                    type=provider_id,
                    priority=100 + index,
                    provider=instance,
                )
            )
        existing_types = {item.type for item in registered}
        for index, (provider_id, provider_cls) in enumerate(_entry_point_providers().items()):
            if provider_id in existing_types:
                continue
            provider_config = _provider_config(raw_config, provider_id)
            if provider_config.get("enabled", True) is False:
                continue
            try:
                instance = await provider_cls.discover(provider_config)
            except LookupError:
                continue
            registered.append(
                RegisteredProvider(
                    name=provider_id,
                    type=provider_id,
                    priority=300 + index,
                    provider=instance,
                )
            )
        configured_profiles = list(load_sandbox_provider_profiles() if profiles is None else profiles)
        if configured_profiles:
            profile_registered = await cls._registered_from_profiles_async(configured_profiles)
            for item in profile_registered:
                registered = [
                    candidate
                    for candidate in registered
                    if candidate.name != item.name
                    and not (candidate.type == item.type and item.name != item.type)
                ]
                registered.append(item)
        return cls(registered)

    @classmethod
    async def discover_from_profiles_async(
        cls,
        profiles: Iterable[SandboxProviderProfile],
    ) -> "ProviderRegistry":
        return cls(await cls._registered_from_profiles_async(profiles))

    @classmethod
    async def _registered_from_profiles_async(
        cls,
        profiles: Iterable[SandboxProviderProfile],
    ) -> list[RegisteredProvider]:
        registered: list[RegisteredProvider] = []
        first_party = FIRST_PARTY_PROVIDERS
        entry_points_map = _entry_point_providers()
        for profile in profiles:
            if not profile.enabled:
                continue
            resolved = profile.with_env_overrides()
            provider_config = resolved.provider_config()
            if profile.type in first_party:
                provider_cls = _load_provider(first_party[profile.type])
                instance = await provider_cls.discover(provider_config)
            elif profile.type in entry_points_map:
                instance = await entry_points_map[profile.type].discover(provider_config)
            else:
                raise LookupError(
                    f"unknown sandbox provider type {profile.type!r} "
                    f"(profile {profile.name!r})"
                )
            registered.append(
                RegisteredProvider(
                    name=profile.name,
                    type=profile.type,
                    priority=profile.priority,
                    provider=instance,
                )
            )
        return registered

    def list(self) -> list[SandboxProviderInfo]:
        return [item.provider.info for item in self._registered]

    def list_registered(self) -> list[RegisteredProvider]:
        return list(self._registered)

    def get(self, key: str) -> SandboxProvider:
        item = self._by_name.get(key) or self._by_type.get(key)
        if item is None:
            raise KeyError(f"unknown sandbox provider {key!r}")
        return item.provider

    def get_registered(self, key: str) -> RegisteredProvider:
        item = self._by_name.get(key) or self._by_type.get(key)
        if item is None:
            raise KeyError(f"unknown sandbox provider {key!r}")
        return item

    async def resolve(
        self,
        *,
        capabilities: frozenset[str],
        spec: Spec,
        region: str | None,
        isolation: Isolation | None,
        network: NetworkMode,
        provider: str | None = None,
        strict_provider: bool = False,
        avoid: frozenset[str] = frozenset(),
    ) -> SandboxProvider:
        chain = await self.resolve_chain(
            capabilities=capabilities,
            spec=spec,
            region=region,
            isolation=isolation,
            network=network,
            provider=provider,
            strict_provider=strict_provider,
            avoid=avoid,
        )
        if not chain:
            raise LookupError("no sandbox provider satisfies the requested spec")
        return chain[0]

    async def resolve_chain(
        self,
        *,
        capabilities: frozenset[str],
        spec: Spec,
        region: str | None,
        isolation: Isolation | None,
        network: NetworkMode,
        provider: str | None = None,
        strict_provider: bool = False,
        avoid: frozenset[str] = frozenset(),
    ) -> list[SandboxProvider]:
        if provider and strict_provider:
            candidates = [self.get_registered(provider)]
        else:
            candidates = list(self._registered)
            if provider:
                preferred = self._by_name.get(provider) or self._by_type.get(provider)
                if preferred is not None:
                    candidates = [preferred, *(item for item in candidates if item is not preferred)]
        if avoid and not strict_provider:
            fresh = [item for item in candidates if item.name not in avoid and item.type not in avoid]
            stale = [item for item in candidates if item.name in avoid or item.type in avoid]
            candidates = [*fresh, *stale]
        chain: list[SandboxProvider] = []
        for candidate in candidates:
            info = candidate.provider.info
            if not info.supports_static(
                capabilities=capabilities,
                spec=spec,
                region=region,
                isolation=isolation,
                network=network,
            ):
                continue
            try:
                supported = await candidate.provider.supports(
                    capabilities, spec, region, isolation, network
                )
            except Exception:
                if strict_provider:
                    raise
                continue
            if supported:
                chain.append(candidate.provider)
        return chain


def _provider_config(config: dict[str, object], provider_id: str) -> dict[str, object]:
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return {}
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        return {}
    return dict(provider)


def _load_provider(path: str) -> type[SandboxProvider]:
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"invalid provider path {path!r}")
    module = importlib.import_module(module_name)
    loaded = getattr(module, attr)
    return loaded


def _entry_point_providers() -> dict[str, type[SandboxProvider]]:
    discovered: dict[str, type[SandboxProvider]] = {}
    for entry_point in entry_points(group="trunks.sandboxes.providers"):
        loaded: Any = entry_point.load()
        discovered[entry_point.name] = loaded
    return discovered
