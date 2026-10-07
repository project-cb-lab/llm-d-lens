"""Pluggable identity providers (design section 20)."""

from llm_d_bench.auth.providers.base import (
    ConnectionResult,
    ExternalIdentity,
    IdentityProvider,
    IdentityProviderError,
    ProviderCapabilities,
)
from llm_d_bench.auth.providers.ldap import LdapProvider, escape_filter_value
from llm_d_bench.auth.providers.local import LocalProvider
from llm_d_bench.auth.providers.registry import get_provider, provider_class, register, registered_types

__all__ = [
    "ConnectionResult",
    "ExternalIdentity",
    "IdentityProvider",
    "IdentityProviderError",
    "LdapProvider",
    "LocalProvider",
    "ProviderCapabilities",
    "escape_filter_value",
    "get_provider",
    "provider_class",
    "register",
    "registered_types",
]
