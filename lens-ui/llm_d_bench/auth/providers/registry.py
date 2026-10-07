"""Identity-provider type registry (design section 20.2)."""

from __future__ import annotations

from llm_d_bench.auth.providers.base import IdentityProvider, IdentityProviderError
from llm_d_bench.auth.providers.ldap import LdapProvider
from llm_d_bench.auth.providers.local import LocalProvider
from llm_d_bench.auth.records import IdentityProviderRecord

_REGISTRY: dict[str, type[IdentityProvider]] = {}


def register(provider_class: type[IdentityProvider]) -> type[IdentityProvider]:
    _REGISTRY[provider_class.type] = provider_class
    return provider_class


register(LocalProvider)
register(LdapProvider)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


def provider_class(provider_type: str) -> type[IdentityProvider] | None:
    return _REGISTRY.get(provider_type)


def get_provider(record: IdentityProviderRecord, *, secret: str | None = None) -> IdentityProvider:
    cls = _REGISTRY.get(record.type)
    if cls is None:
        raise IdentityProviderError(f"unknown identity provider type: {record.type}")
    return cls(record, secret=secret)
