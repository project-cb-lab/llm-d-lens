"""Lens-managed (local) identity provider marker.

Credential verification for local users lives in ``AuthService`` (it needs the
password hash and lockout state); this class only advertises capabilities so
the registry and login page treat ``local`` like any other provider.
"""

from __future__ import annotations

from typing import ClassVar

from llm_d_bench.auth.providers.base import ExternalIdentity, IdentityProvider, ProviderCapabilities


class LocalProvider(IdentityProvider):
    type: ClassVar[str] = "local"
    capabilities: ClassVar[ProviderCapabilities] = ProviderCapabilities(
        password_auth=True,
        browser_redirect=False,
        group_sync=False,
        writable=True,
        jit_provisioning=False,
    )

    async def authenticate(self, username: str, password: str) -> ExternalIdentity | None:
        return None
