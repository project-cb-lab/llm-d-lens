"""Pluggable identity-provider abstraction (design section 20.2).

New IdP types implement ``IdentityProvider`` and register by ``type``; the
``identity_providers`` table stores per-type configuration as JSON, so adding
a protocol never changes the schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from llm_d_bench.auth.records import IdentityProviderRecord


@dataclass(frozen=True)
class ProviderCapabilities:
    password_auth: bool
    browser_redirect: bool
    group_sync: bool
    writable: bool
    jit_provisioning: bool


@dataclass(frozen=True)
class ExternalIdentity:
    external_id: str
    username: str
    display_name: str = ""
    email: str = ""
    groups: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ConnectionResult:
    ok: bool
    detail: str = ""


class IdentityProviderError(Exception):
    """Raised when an external provider cannot complete an operation."""


class IdentityProvider(ABC):
    type: ClassVar[str]
    capabilities: ClassVar[ProviderCapabilities]

    def __init__(self, record: IdentityProviderRecord, *, secret: str | None = None) -> None:
        self.record = record
        self.secret = secret
        self.config = dict(record.config or {})

    @abstractmethod
    async def authenticate(self, username: str, password: str) -> ExternalIdentity | None:
        """Return the identity when credentials are valid, else ``None``."""

    async def lookup(self, external_id: str) -> ExternalIdentity | None:
        return None

    async def search_users(self, query: str) -> list[ExternalIdentity]:
        return []

    async def list_groups(self) -> list[dict[str, str]]:
        """Enumerate directory groups as ``{"external_id", "name"}`` records.

        ``external_id`` is the stable directory identifier (LDAP DN); ``name`` is
        the human label. Providers without a group concept return an empty list.
        """
        return []

    async def test_connection(self) -> ConnectionResult:
        return ConnectionResult(ok=False, detail="not implemented")
